"""
services/vision/vision/bus/mqtt.py

The vision service's link to the rest of the stack. See docs/mqtt-contract.md.

Design rules
  * The bus is a convenience, not a control path. Counting and the run permit never wait on
    it: publishing is fire-and-forget (QoS 0 for state, QoS 1 for events and acks) and the
    client reconnects on its own.
  * `state` is published retained, so a UI that opens later immediately sees the current
    state instead of a blank screen.
  * A last-will message marks the state topic OFFLINE if this process dies, so the UI can
    say "vision service not running" rather than showing a stale count as if it were live.
  * Commands are answered on `ack` with the caller's request_id, including rejections. The
    UI therefore learns *why* a start was refused ("select a crate first", "stop button
    active") instead of silently doing nothing.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import paho.mqtt.client as mqtt

from vision.config import MqttCfg

log = logging.getLogger("bus")

# name -> required payload keys
COMMANDS: Dict[str, Tuple[str, ...]] = {
    "select_batch": ("crate", "target"),
    "cancel_batch": (),
    "start": (),
    "stop": (),
    "finish_batch": (),
    "reset_fault": (),
}

CommandHandler = Callable[[str, Dict[str, Any]], Tuple[bool, str]]


class MqttBus:
    def __init__(self, cfg: MqttCfg, client_id: str = "vision",
                 on_command: Optional[CommandHandler] = None):
        self.cfg = cfg
        self.base = cfg.base_topic.rstrip("/")
        self.on_command = on_command
        self._connected = threading.Event()
        self._disconnected_since: Optional[float] = time.monotonic()
        self._lock = threading.Lock()
        self._started = False

        # paho 2.x requires an explicit callback API version; 1.x does not know the enum.
        try:
            self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id, clean_session=True)
        except AttributeError:  # paho-mqtt 1.x
            self._c = mqtt.Client(client_id=client_id, clean_session=True)

        if cfg.username:
            self._c.username_pw_set(cfg.username, cfg.password or None)
        self._c.reconnect_delay_set(min_delay=1, max_delay=10)
        self._c.will_set(self.t_state, json.dumps({"state": "OFFLINE", "reason": "vision service stopped"}),
                         qos=1, retain=True)
        self._c.on_connect = self._on_connect
        self._c.on_disconnect = self._on_disconnect
        self._c.on_message = self._on_message

    # ── topics ──
    @property
    def t_state(self) -> str:
        return f"{self.base}/state"

    @property
    def t_event(self) -> str:
        return f"{self.base}/event"

    @property
    def t_ack(self) -> str:
        return f"{self.base}/ack"

    @property
    def t_cmd(self) -> str:
        return f"{self.base}/cmd"

    # ── lifecycle ──
    def start(self) -> None:
        """Non-blocking: a broker that is not up yet does not delay the vision service."""
        self._started = True
        try:
            self._c.connect_async(self.cfg.host, self.cfg.port, keepalive=self.cfg.keepalive_s)
        except Exception as e:  # bad host name, etc.
            log.error("MQTT connect_async failed: %s", e)
        self._c.loop_start()
        log.info("MQTT client started for %s:%d (base topic '%s')", self.cfg.host, self.cfg.port, self.base)

    def stop(self, offline_state: Optional[Dict[str, Any]] = None) -> None:
        if not self._started:
            return
        try:
            payload = offline_state or {"state": "OFFLINE", "reason": "vision service stopped"}
            self._c.publish(self.t_state, json.dumps(payload, default=str), qos=1, retain=True)
            time.sleep(0.1)  # give the publish a chance to leave before the socket closes
            self._c.disconnect()
        except Exception:
            pass
        finally:
            self._c.loop_stop()
            self._started = False

    # ── status ──
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def offline_for(self, now: Optional[float] = None) -> float:
        """Seconds since the broker connection was lost (0.0 while connected)."""
        if self._connected.is_set():
            return 0.0
        since = self._disconnected_since
        return 0.0 if since is None else (now or time.monotonic()) - since

    # ── publishing ──
    def publish_state(self, state: Dict[str, Any]) -> None:
        self._publish(self.t_state, state, qos=0, retain=True)

    def publish_event(self, name: str, data: Dict[str, Any]) -> None:
        payload = dict(data)
        payload["event"] = name
        payload.setdefault("ts", time.time())
        self._publish(self.t_event, payload, qos=1, retain=False)

    def publish_ack(self, request_id: Optional[str], command: str, ok: bool, reason: str = "") -> None:
        self._publish(self.t_ack, {"request_id": request_id, "command": command,
                                   "ok": ok, "reason": reason, "ts": time.time()}, qos=1, retain=False)

    def _publish(self, topic: str, payload: Dict[str, Any], qos: int, retain: bool) -> None:
        try:
            self._c.publish(topic, json.dumps(payload, default=str), qos=qos, retain=retain)
        except Exception as e:
            log.debug("publish to %s failed: %s", topic, e)

    # ── callbacks ──
    def _on_connect(self, client, userdata, flags, rc, *args) -> None:
        if rc != 0:
            log.error("MQTT connection refused (rc=%s)", rc)
            return
        self._connected.set()
        self._disconnected_since = None
        client.subscribe(f"{self.t_cmd}/#", qos=1)
        log.info("MQTT connected; subscribed to %s/#", self.t_cmd)

    def _on_disconnect(self, client, userdata, rc, *args) -> None:
        self._connected.clear()
        self._disconnected_since = time.monotonic()
        log.warning("MQTT disconnected (rc=%s); reconnecting in the background", rc)

    def _on_message(self, client, userdata, msg) -> None:
        """Runs on the paho network thread. The handler takes the controller lock itself."""
        command = msg.topic.rsplit("/", 1)[-1]
        request_id = None
        try:
            payload = json.loads(msg.payload.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            request_id = payload.get("request_id")

            if command not in COMMANDS:
                self.publish_ack(request_id, command, False, f"unknown command '{command}'")
                return
            missing = [k for k in COMMANDS[command] if k not in payload]
            if missing:
                self.publish_ack(request_id, command, False, f"missing field(s): {', '.join(missing)}")
                return
            if self.on_command is None:
                self.publish_ack(request_id, command, False, "no command handler")
                return

            ok, reason = self.on_command(command, payload)
            self.publish_ack(request_id, command, ok, reason)
        except Exception as e:  # noqa: BLE001 - a malformed command must not kill the client
            log.error("bad command on %s: %s", msg.topic, e)
            self.publish_ack(request_id, command, False, f"bad command: {e}")
