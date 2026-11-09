"""
services/api/api/bus.py

MQTT side of the API (see docs/mqtt-contract.md).

  * Subscribes to counter/state, counter/event and counter/ack and fans every message out to the
    connected WebSocket clients. Each client has a small bounded queue; a slow browser loses old
    messages, it never slows the bus or the other clients.
  * Sends commands to the vision service and waits for its ack (matched by request_id), so the
    UI gets the controller's own answer ("select a crate and target first", ...) or a clear
    timeout ("vision service did not answer") instead of a button that silently does nothing.
  * Remembers the last state. `state_live` is False for a retained message delivered on
    subscribe: the UI must not show an old retained count as if it were live.

The API is not on the control path. If it dies, counting and the conveyor interlock carry on.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional, Set, Tuple

import paho.mqtt.client as mqtt

log = logging.getLogger("api.bus")

CLIENT_QUEUE = 64          # messages buffered per WebSocket client
STATE_FRESH_S = 2.0        # vision publishes state at ~5 Hz; older than this is stale


class BusUnavailable(RuntimeError):
    """The broker is not connected: the command was not sent."""


class CommandTimeout(RuntimeError):
    """The command was sent but the vision service did not answer in time."""


class _Subscriber:
    __slots__ = ("loop", "queue")

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE)


def _put_latest(q: asyncio.Queue, msg: Dict[str, Any]) -> None:
    """Runs on the event loop. Drop the oldest message rather than block."""
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(msg)


class LiveBus:
    def __init__(self, host: str, port: int, base_topic: str, keepalive_s: int = 15,
                 username: Optional[str] = None, password: Optional[str] = None,
                 client_id: str = "api"):
        self.host, self.port, self.keepalive_s = host, port, keepalive_s
        self.base = base_topic.rstrip("/")
        self._lock = threading.Lock()
        self._subs: Set[_Subscriber] = set()
        self._pending: Dict[str, Tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
        self._connected = threading.Event()
        self._started = False

        self.state: Optional[Dict[str, Any]] = None
        self.state_rx: Optional[float] = None      # monotonic time of the last state message
        self.state_live = False                    # False = retained message from before we connected

        try:  # paho 2.x needs an explicit callback API version; 1.x does not know the enum
            self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id, clean_session=True)
        except AttributeError:
            self._c = mqtt.Client(client_id=client_id, clean_session=True)
        if username:
            self._c.username_pw_set(username, password or None)
        self._c.reconnect_delay_set(min_delay=1, max_delay=10)
        self._c.on_connect = self._on_connect
        self._c.on_disconnect = self._on_disconnect
        self._c.on_message = self._on_message

    # ── topics ──
    def topic(self, leaf: str) -> str:
        return f"{self.base}/{leaf}"

    # ── lifecycle ──
    def start(self) -> None:
        self._started = True
        try:
            self._c.connect_async(self.host, self.port, keepalive=self.keepalive_s)
        except Exception as e:  # noqa: BLE001
            log.error("MQTT connect_async failed: %s", e)
        self._c.loop_start()
        log.info("MQTT client started for %s:%d (base topic '%s')", self.host, self.port, self.base)

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._c.disconnect()
        finally:
            self._c.loop_stop()
            self._started = False

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def state_fresh(self, now: Optional[float] = None) -> bool:
        if not self.state_live or self.state_rx is None:
            return False
        return ((now or time.monotonic()) - self.state_rx) <= STATE_FRESH_S

    # ── WebSocket fan-out ──
    def subscribe(self) -> _Subscriber:
        sub = _Subscriber(asyncio.get_running_loop())
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    def broadcast(self, msg: Dict[str, Any]) -> None:
        """Thread-safe: callable from the paho thread or from request handlers."""
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            try:
                s.loop.call_soon_threadsafe(_put_latest, s.queue, msg)
            except RuntimeError:  # loop closed: client is going away
                pass

    def state_message(self) -> Optional[Dict[str, Any]]:
        if self.state is None:
            return None
        return {"type": "state", "data": self.state, "live": self.state_fresh()}

    # ── commands ──
    async def command(self, name: str, payload: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
        if not self.connected:
            raise BusUnavailable("message bus (MQTT broker) is not connected")
        rid = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        with self._lock:
            self._pending[rid] = (loop, fut)
        body = dict(payload)
        body.update({"request_id": rid, "source": body.get("source", "ui")})
        try:
            info = self._c.publish(self.topic(f"cmd/{name}"), json.dumps(body), qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise BusUnavailable(f"publish failed (rc={info.rc})")
            return await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError:
            raise CommandTimeout(f"vision service did not answer '{name}' within {timeout_s:.0f} s") from None
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    # ── paho callbacks (paho network thread) ──
    def _on_connect(self, client, userdata, flags, rc, *args) -> None:
        if rc != 0:
            log.error("MQTT connection refused (rc=%s)", rc)
            return
        self._connected.set()
        for leaf in ("state", "event", "ack"):
            client.subscribe(self.topic(leaf), qos=1)
        log.info("MQTT connected")
        self.broadcast({"type": "bus", "connected": True})

    def _on_disconnect(self, client, userdata, rc, *args) -> None:
        self._connected.clear()
        log.warning("MQTT disconnected (rc=%s); reconnecting in the background", rc)
        self.broadcast({"type": "bus", "connected": False})

    def _on_message(self, client, userdata, msg) -> None:
        try:
            data = json.loads(msg.payload.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            log.warning("ignoring non-JSON message on %s", msg.topic)
            return
        leaf = msg.topic.rsplit("/", 1)[-1]
        if leaf == "state":
            self.state = data
            self.state_rx = time.monotonic()
            self.state_live = not msg.retain
            self.broadcast({"type": "state", "data": data, "live": self.state_live})
        elif leaf == "event":
            self.broadcast({"type": "event", "data": data})
        elif leaf == "ack":
            rid = data.get("request_id")
            with self._lock:
                waiter = self._pending.get(rid) if rid else None
            if waiter:
                loop, fut = waiter
                loop.call_soon_threadsafe(lambda f=fut, d=data: f.done() or f.set_result(d))
            self.broadcast({"type": "ack", "data": data})
