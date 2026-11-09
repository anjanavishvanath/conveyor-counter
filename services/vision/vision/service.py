"""
services/vision/vision/service.py

The vision service: the only process that counts and that decides whether the conveyor
may run. It runs whether or not a browser, an API or a broker is up.

Threads
  main          camera -> detector -> tracker -> counter, then hands counts to the controller
  io            reads the buttons, runs the controller's timing checks, writes relay + lamp
                (vision/io/runner.py) at ~50 Hz, independent of the camera loop
  store-writer  serialises database writes so the disk never stalls counting
  paho          MQTT network loop; command callbacks take the controller lock
  preview       MJPEG HTTP server (optional)

Every controller call happens under one lock, which is what makes the above safe.

Failure behaviour
  camera dies / frames stop      io thread sees the frame timeout -> FAULT -> permit OFF
  inference stalls               same path (no frames arriving) + low-fps check
  lost frames (frame-id gap)     counted, and a FAULT above safety.max_frame_gap
  database writes failing        FAULT if safety.fault_on_db_error (counts not persisted)
  broker down                    FAULT only if safety.fault_on_bus_loss, after bus_grace_s
  io thread stops cycling        main loop notices and FAULTs
  this process dies              nothing holds the relay energized -> conveyor stops
  board dies                     same, plus the external watchdog of checkpoint 10

A restart with a batch still open re-adopts that batch, marks it uncertain and waits for a
human START. Nothing ever restarts the conveyor by itself.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any, Dict, Optional, Tuple

from vision.batch.controller import Batch, Controller, State
from vision.bus.mqtt import MqttBus
from vision.config import AppConfig
from vision.detectors.factory import create_detector
from vision.draw import annotate
from vision.io.factory import create_io
from vision.io.runner import IoRunner
from vision.pipeline import CountingPipeline
from vision.preview.mjpeg import PreviewServer
from vision.sources.base import FrameSource, SourceError
from vision.sources.video_file import VideoFileSource
from vision.store.db import Store, StoreWriter

log = logging.getLogger("service")

STATE_PUBLISH_HZ = 5.0
BATCH_SYNC_PERIOD_S = 10.0     # periodic full batch row write (frames_lost, faults, stops)
IO_STALL_TIMEOUT_S = 1.0       # io thread has not cycled for this long -> fault
SOURCE_RETRY_S = 2.0


def create_source(cfg: AppConfig, stride: int = 1) -> FrameSource:
    fs = cfg.site.frame_source
    if fs.type == "video_file":
        v = fs.video_file
        return VideoFileSource(v.path, realtime=v.realtime, loop=v.loop, stride=stride)
    if fs.type == "hik_mvs":
        from vision.sources.hik_mvs import HikMvsSource  # added at board bring-up
        return HikMvsSource(fs.hik_mvs)
    raise ValueError(f"Unknown frame source: {fs.type}")


class VisionService:
    def __init__(self, cfg: AppConfig, exit_on_end: bool = False, stride: int = 1):
        self.cfg = cfg
        self.exit_on_end = exit_on_end
        self.stride = stride

        self.lock = threading.Lock()          # guards every Controller call
        self._stop = threading.Event()

        self.store: Optional[Store] = None
        self.writer: Optional[StoreWriter] = None
        self.bus: Optional[MqttBus] = None
        self.preview: Optional[PreviewServer] = None
        self.source: Optional[FrameSource] = None
        self.pipeline: Optional[CountingPipeline] = None
        self.ctrl: Optional[Controller] = None
        self.io_runner: Optional[IoRunner] = None

        self._detector_name = cfg.site.detector.backend
        self._device = "cpu"
        self._last_state_pub = 0.0
        self._last_batch_sync = 0.0
        self._frames = 0
        self._infer_ms = 0.0
        self._source_errors = 0

    # ───────────────────────── setup ─────────────────────────
    def setup(self) -> None:
        s = self.cfg.site

        self.store = Store(s.storage.db_path, synchronous=s.storage.synchronous)
        self.store.migrate()
        self.writer = StoreWriter(self.store, on_error=self._on_store_error)
        self.writer.start()

        self.ctrl = Controller(
            crates=s.crates,
            safety=s.safety,
            alarm_flash_on_fault=s.io.alarm_flash_on_fault,
            on_event=self._on_controller_event,
        )

        io_backend = create_io(s.io)
        self.io_runner = IoRunner(io_backend, self.ctrl, self.lock,
                                  debounce_ms=s.io.debounce_ms, period_s=0.02)

        detector = create_detector(self.cfg)
        self._device = detector.device
        self.pipeline = CountingPipeline(self.cfg.profile, detector)

        self.source = create_source(self.cfg, stride=self.stride)
        self.source.open()
        log.info("frame source: %s (%.1f fps nominal)", s.frame_source.type, self.source.nominal_fps)

        self.bus = MqttBus(s.mqtt, client_id=f"vision-{s.site_name}".replace(" ", "-"),
                           on_command=self._on_command)
        self.bus.start()

        self.preview = PreviewServer(s.preview)
        self.preview.start()

        self._recover()
        self.io_runner.start()
        log.info("vision service ready: detector=%s on %s, product=%s, %d crates",
                 self._detector_name, self._device, self.cfg.profile.product, s.crates.count)

    def _recover(self) -> None:
        """Re-adopt a batch that was open when the service last stopped."""
        row = self.store.open_batch()
        if row is None:
            return
        stale = self.store.abort_open_batches(except_id=row["batch_id"])
        if stale:
            log.warning("aborted %d older open batch row(s)", stale)
        batch = Batch.from_row(row)
        now = time.monotonic()
        with self.lock:
            ok, reason = self.ctrl.restore_batch(batch, now)
        if ok:
            self.store.mark_recovered(batch.batch_id)
            log.warning("recovered batch %s: crate %d, %d/%d counted, state %s - marked UNCERTAIN, "
                        "waiting for operator START", batch.batch_id[:8], batch.crate,
                        batch.count, batch.target, self.ctrl.state.value)
        else:
            log.error("could not recover open batch %s: %s", row["batch_id"], reason)

    # ───────────────────────── hooks ─────────────────────────
    def _on_controller_event(self, name: str, data: Dict[str, Any]) -> None:
        """Called under self.lock by the controller. Keep it non-blocking: enqueue only."""
        ts = time.time()
        batch_id = data.get("batch_id")
        try:
            self.writer.log_event(name, ts, batch_id, data)
            if name == "count":
                self.writer.save_count(batch_id, int(data["count"]), int(data["added"]), ts)
            elif "batch" in data:
                self.writer.save_batch(data["batch"], **self._batch_extra())
            if name in ("state_changed", "fault", "target_reached") and self.ctrl.batch is not None:
                self.writer.save_batch(self._batch_dict(), **self._batch_extra())
            if name in ("batch_completed", "batch_aborted"):
                log.info("%s: %s", name, {k: data["batch"].get(k) for k in
                                          ("crate", "target", "count", "uncertain", "frames_lost")})
            if name == "fault":
                log.error("FAULT: %s", data.get("reason"))
        except Exception:
            log.exception("event hook failed for %s", name)
        if self.bus is not None:
            self.bus.publish_event(name, data)

    def _batch_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self.ctrl.batch)

    def _batch_extra(self) -> Dict[str, Any]:
        return {
            "site_name": self.cfg.site.site_name,
            "product": self.cfg.profile.product,
            "product_profile": str(self.cfg.profile.path or ""),
            "model_file": str(self.cfg.profile.model.file.name),
            "detector": f"{self._detector_name}/{self._device}",
        }

    def _on_store_error(self, exc: Exception) -> None:
        if not self.cfg.site.safety.fault_on_db_error:
            return
        with self.lock:
            self.ctrl.fault(time.monotonic(), "db_error")

    def _on_command(self, command: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
        """Called on the paho thread. Takes the controller lock."""
        now = time.monotonic()
        source = str(payload.get("source", "ui"))
        try:
            with self.lock:
                if command == "select_batch":
                    return self.ctrl.select_batch(int(payload["crate"]), int(payload["target"]))
                if command == "cancel_batch":
                    return self.ctrl.cancel_batch()
                if command == "start":
                    return self.ctrl.request_start(now, source=source)
                if command == "stop":
                    return self.ctrl.request_stop(now, source=source)
                if command == "finish_batch":
                    return self.ctrl.finish_batch(now)
                if command == "reset_fault":
                    return self.ctrl.reset_fault(now)
            return False, f"unhandled command '{command}'"
        except (TypeError, ValueError) as e:
            return False, f"bad argument: {e}"
        except Exception as e:  # noqa: BLE001
            log.exception("command %s failed", command)
            return False, f"internal error: {e}"

    # ───────────────────────── state publishing ─────────────────────────
    def _snapshot(self, now: float) -> Dict[str, Any]:
        with self.lock:
            snap = self.ctrl.snapshot(now)
        stats = self.pipeline.counter.stats
        snap.update({
            "site_name": self.cfg.site.site_name,
            "product": self.cfg.profile.product,
            "detector": self._detector_name,
            "device": self._device,
            "infer_ms": round(self._infer_ms, 1),
            "frames": self._frames,
            "lifetime_total": stats.total,
            "suspects": stats.suspects,
            "suspect_detail": stats.as_dict(),
            "bus_connected": bool(self.bus and self.bus.connected),
            "store_backlog": self.writer.backlog if self.writer else 0,
            "store_dropped": self.writer.dropped if self.writer else 0,
            "preview_url": (f"http://{self.cfg.site.preview.host}:{self.cfg.site.preview.port}/preview.mjpg"
                            if self.cfg.site.preview.enabled else None),
            "crates": self.cfg.site.crates.count,
            "default_target": self.cfg.site.crates.default_target,
            "max_target": self.cfg.site.crates.max_target,
            "ts": time.time(),
        })
        return snap

    def _periodic(self, now: float) -> None:
        # 1. the io thread must keep cycling; it is what drops the permit on a camera stall
        if self.io_runner.last_cycle_t and now - self.io_runner.last_cycle_t > IO_STALL_TIMEOUT_S:
            with self.lock:
                self.ctrl.fault(now, "io_thread_stalled")

        # 2. broker loss: visible in the state, a fault only if the plant asked for that
        safety = self.cfg.site.safety
        if safety.fault_on_bus_loss and self.bus is not None:
            if self.bus.offline_for(now) > safety.bus_grace_s:
                with self.lock:
                    if self.ctrl.state != State.FAULT:
                        self.ctrl.fault(now, "bus_offline")

        # 3. state publish
        if now - self._last_state_pub >= 1.0 / STATE_PUBLISH_HZ:
            self._last_state_pub = now
            if self.bus is not None:
                self.bus.publish_state(self._snapshot(now))

        # 4. keep the open batch row current (frames_lost, faults, stops)
        if now - self._last_batch_sync >= BATCH_SYNC_PERIOD_S:
            self._last_batch_sync = now
            with self.lock:
                has_batch = self.ctrl.batch is not None
                row = self._batch_dict() if has_batch else None
            if row:
                self.writer.save_batch(row, **self._batch_extra())

    # ───────────────────────── main loop ─────────────────────────
    def run(self) -> None:
        timeout_s = max(0.2, self.cfg.site.safety.frame_timeout_ms / 1000)
        ended = False
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                frame = self.source.read(timeout_s=timeout_s)
                self._source_errors = 0
            except SourceError as e:
                self._source_errors += 1
                log.error("frame source error (%d): %s", self._source_errors, e)
                with self.lock:
                    self.ctrl.fault(time.monotonic(), "source_error")
                self._reopen_source()
                self._periodic(time.monotonic())
                continue

            if frame is None:  # clean end of a video file
                if not ended:
                    ended = True
                    log.info("end of stream")
                    with self.lock:
                        self.ctrl.fault(time.monotonic(), "source_eos")
                if self.exit_on_end:
                    break
                self._periodic(time.monotonic())
                self._stop.wait(0.2)
                continue

            result = self.pipeline.process(frame)           # outside the lock: the expensive part
            now = time.monotonic()
            self._frames += 1
            self._infer_ms = result.timings_ms.get("inference", self._infer_ms)

            with self.lock:
                self.ctrl.on_frame(frame.frame_id, now)
                if result.new_counts:
                    self.ctrl.on_counts(len(result.new_counts), now)

            if self.preview is not None and self.preview.should_publish(now):
                counted = set(self.pipeline.counter.stats.counted_ids)
                with self.lock:
                    b = self.ctrl.batch
                    extra = (f"{self.ctrl.state.value}  crate {b.crate}  {b.count}/{b.target}"
                             if b else self.ctrl.state.value)
                self.preview.publish(annotate(result, self.cfg.profile.counting, counted, extra), now)

            self._periodic(now)

    def _reopen_source(self) -> None:
        """Try to reopen the camera. A FAULT is already set; it needs an operator reset anyway."""
        try:
            self.source.close()
        except Exception:
            pass
        self._stop.wait(SOURCE_RETRY_S)
        if self._stop.is_set():
            return
        try:
            self.source.open()
            log.info("frame source reopened")
        except SourceError as e:
            log.error("reopen failed: %s", e)

    # ───────────────────────── shutdown ─────────────────────────
    def request_stop(self, *_: Any) -> None:
        log.info("shutdown requested")
        self._stop.set()

    def shutdown(self) -> None:
        log.info("shutting down")
        if self.io_runner is not None:
            self.io_runner.stop()          # drops the permit, alarm on, closes the IO backend
        if self.preview is not None:
            self.preview.stop()
        if self.source is not None:
            try:
                self.source.close()
            except Exception:
                pass
        if self.writer is not None:
            # An open batch stays 'open' on purpose: the next start recovers it.
            with self.lock:
                row = self._batch_dict() if self.ctrl and self.ctrl.batch else None
            if row:
                self.writer.save_batch(row, **self._batch_extra())
            self.writer.flush(timeout=5.0)
            self.writer.stop()
        if self.bus is not None:
            self.bus.stop()
        if self.store is not None:
            self.store.close()
        log.info("stopped")


def run_service(cfg: AppConfig, exit_on_end: bool = False, stride: int = 1) -> int:
    svc = VisionService(cfg, exit_on_end=exit_on_end, stride=stride)
    signal.signal(signal.SIGINT, svc.request_stop)
    signal.signal(signal.SIGTERM, svc.request_stop)
    try:
        svc.setup()
        svc.run()
        return 0
    except Exception:
        log.exception("vision service failed")
        return 1
    finally:
        svc.shutdown()
