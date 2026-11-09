"""
services/vision/tests/test_service.py — the vision service wired together (checkpoint 4),
with a synthetic frame source and a stub detector. No camera, no model, no broker.

This test is about wiring and failure behaviour, not detection quality:
  * a batch runs select -> start -> counting -> target -> clearing -> complete, and the
    final count (overfill included) is persisted;
  * the run permit is ON only while RUNNING;
  * a camera that slows down past the frame timeout drops the permit from the IO thread,
    with no help from the main loop;
  * a restart re-adopts the open batch, marks it uncertain and leaves the conveyor stopped.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict

import numpy as np
import pytest

from vision import service as service_mod
from vision.batch.controller import Batch, State
from vision.config import (AppConfig, CountingCfg, CratesCfg, DetectionCfg, DetectorCfg,
                           FrameSourceCfg, IoCfg, ModelCfg, MqttCfg, PreviewCfg, ProductProfile,
                           SafetyCfg, SiteConfig, StorageCfg, TrackingCfg, VideoFileCfg)
from vision.service import VisionService
from vision.sources.base import FrameSource
from vision.store.db import Store
from vision.types import Detections, Frame

H, W = 240, 320
BOX = 40.0


# ───────────────────────── stubs ─────────────────────────
class ScriptedSource(FrameSource):
    def __init__(self):
        self.frame_id = 0
        self.delay = 0.002          # raise above the frame timeout to simulate a stalling camera
        self.image = np.zeros((H, W, 3), np.uint8)

    def open(self):
        pass

    def read(self, timeout_s: float = 1.0):
        time.sleep(self.delay)
        self.frame_id += 1
        return Frame(image=self.image, frame_id=self.frame_id, timestamp=self.frame_id / 30.0)

    def close(self):
        pass

    @property
    def nominal_fps(self) -> float:
        return 30.0


class MovingDetector:
    """`objects` boxes travelling upstream->downstream, each crossing the line once per pass."""

    def __init__(self):
        self.y: dict[int, float] = {}
        self.objects = 0
        self.speed = 12.0

    def detect(self, image_bgr):
        boxes, scores, cls = [], [], []
        for lane in range(self.objects):
            y = self.y.get(lane, H + 20.0 + lane * 30) - self.speed
            if y < -BOX:
                y = H + BOX
            self.y[lane] = y
            x = 40.0 + lane * 60
            boxes.append([x, y, x + BOX, y + BOX])
            scores.append(0.9)
            cls.append(0)
        if not boxes:
            return Detections.empty()
        return Detections(np.array(boxes, np.float32), np.array(scores, np.float32),
                          np.array(cls, np.int32))

    @property
    def last_timings_ms(self):
        return {"inference": 3.0}

    @property
    def device(self):
        return "cpu"


class FakeBus:
    """Stands in for MqttBus: records what the UI would have seen."""

    def __init__(self, cfg, client_id="vision", on_command=None):
        self.on_command = on_command
        self.states: list[dict] = []
        self.events: list[tuple[str, dict]] = []
        self.connected = True

    def start(self):
        pass

    def stop(self, offline_state=None):
        pass

    def offline_for(self, now=None):
        return 0.0

    def publish_state(self, state):
        self.states.append(state)

    def publish_event(self, name, data):
        self.events.append((name, data))

    def publish_ack(self, *a, **k):
        pass


def make_cfg(tmp_path, **safety) -> AppConfig:
    profile = ProductProfile(
        product="testprod",
        model=ModelCfg(file=tmp_path / "model.onnx", class_names=["thing"]),
        detection=DetectionCfg(),
        tracking=TrackingCfg(min_hits=2, max_age_frames=5),
        counting=CountingCfg(axis="y", line_position=0.5, direction="negative", min_track_hits=2),
    )
    site = SiteConfig(
        site_name="test",
        timezone="Asia/Colombo",
        product_profile=tmp_path / "profile.yaml",
        crates=CratesCfg(count=12, default_target=400, max_target=2000),
        frame_source=FrameSourceCfg(type="video_file", video_file=VideoFileCfg(path=tmp_path / "v.mp4")),
        detector=DetectorCfg(backend="onnx_cpu"),
        io=IoCfg(backend="sim", debounce_ms=0),
        safety=SafetyCfg(frame_timeout_ms=300, max_frame_gap=2, min_fps=1.0, min_fps_window_s=1.0,
                         clear_time_s=0.4, fault_on_bus_loss=False, **safety),
        storage=StorageCfg(db_path=tmp_path / "counter.db", synchronous="NORMAL"),
        mqtt=MqttCfg(host="127.0.0.1", port=1883),
        preview=PreviewCfg(enabled=False),
    )
    return AppConfig(site=site, profile=profile)


@pytest.fixture()
def running(tmp_path, monkeypatch):
    """A started service with its main loop on a thread, plus the stubs to drive it."""
    cfg = make_cfg(tmp_path)
    source, detector = ScriptedSource(), MovingDetector()
    monkeypatch.setattr(service_mod, "create_source", lambda c, stride=1: source)
    monkeypatch.setattr(service_mod, "create_detector", lambda c: detector)
    monkeypatch.setattr(service_mod, "MqttBus", FakeBus)

    svc = VisionService(cfg)
    svc.setup()
    thread = threading.Thread(target=svc.run, name="main-loop", daemon=True)
    thread.start()
    try:
        yield svc, source, detector
    finally:
        svc.request_stop()
        thread.join(timeout=5)
        svc.shutdown()


def wait_until(pred, timeout=6.0, interval=0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


# ───────────────────────── tests ─────────────────────────
def test_full_batch_is_counted_persisted_and_the_permit_follows_the_state(running):
    svc, source, detector = running
    io = svc.io_runner.backend

    assert wait_until(lambda: svc._frames > 5), "no frames processed"
    assert svc._on_command("select_batch", {"crate": 3, "target": 5})[0]
    assert svc.ctrl.state is State.READY
    assert io.run_permit is False, "permit must stay off until START"

    ok, reason = svc._on_command("start", {"source": "ui"})
    assert ok, reason
    assert wait_until(lambda: io.run_permit is True), "permit should be energized in RUNNING"

    detector.objects = 2                                  # objects start moving down the belt
    assert wait_until(lambda: svc.ctrl.state is State.CLEARING, timeout=8), "target not reached"
    assert wait_until(lambda: io.run_permit is False), "permit must drop once the target is reached"

    batch_id = svc.ctrl.batch.batch_id
    detector.objects = 0                                  # the belt empties into the open crate
    assert wait_until(lambda: svc.ctrl.state is State.IDLE, timeout=8), "clearing never finished"

    assert svc.writer.flush(timeout=5)
    row = svc.store.get_batch(batch_id)
    assert row["status"] == "completed"
    assert row["count"] >= 5                              # target plus whatever cleared after it
    assert row["crate"] == 3 and row["target"] == 5
    assert row["product"] == "testprod" and row["detector"] == "onnx_cpu/cpu"
    assert row["overfill"] == row["count"] - 5

    counts = svc.store._conn().execute(
        "SELECT count, added FROM counts WHERE batch_id=? ORDER BY count", (batch_id,)).fetchall()
    assert [c["count"] for c in counts] == list(range(1, row["count"] + 1))

    kinds = [k for k, _ in svc.bus.events]
    for expected in ("batch_selected", "count", "target_reached", "batch_completed"):
        assert expected in kinds, f"{expected} was never published"
    assert svc.bus.states, "no state was published for the UI"
    assert svc.bus.states[-1]["crates"] == 12


def test_a_stalling_camera_drops_the_permit_from_the_io_thread(running):
    svc, source, detector = running
    io = svc.io_runner.backend

    assert wait_until(lambda: svc._frames > 5)
    assert svc._on_command("select_batch", {"crate": 1, "target": 400})[0]
    assert svc._on_command("start", {})[0]
    assert wait_until(lambda: io.run_permit is True)

    source.delay = 0.6                                    # frames now arrive slower than frame_timeout_ms
    assert wait_until(lambda: svc.ctrl.state is State.FAULT, timeout=6), "no fault on a stalled camera"
    assert wait_until(lambda: io.run_permit is False), "a fault must drop the permit"
    assert any(r.startswith("frame_timeout") or r.startswith("low_fps")
               for r in svc.ctrl.fault_reasons), svc.ctrl.fault_reasons

    # A fault is not self-clearing and never restarts the conveyor.
    assert svc._on_command("start", {})[0] is False
    source.delay = 0.002
    assert wait_until(lambda: svc._on_command("reset_fault", {})[0], timeout=6)
    assert svc.ctrl.state is State.PAUSED                 # back to a stopped batch, awaiting START
    assert wait_until(lambda: io.run_permit is False), "resetting a fault must not restart the conveyor"
    assert svc.ctrl.batch.uncertain is True               # the batch carries the fault for review


def test_restart_recovers_the_open_batch_and_stays_stopped(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.site.storage.db_path, synchronous="NORMAL")
    store.migrate()
    store.save_batch(asdict(Batch(batch_id="b-open", crate=9, target=400, count=137,
                                  created_at=time.time() - 300, started_at=time.time() - 290)),
                     site_name="test", product="testprod")
    store.save_batch(asdict(Batch(batch_id="b-older", crate=4, target=400, count=20,
                                  created_at=time.time() - 9000)), site_name="test", product="testprod")
    store.close()

    monkeypatch.setattr(service_mod, "create_source", lambda c, stride=1: ScriptedSource())
    monkeypatch.setattr(service_mod, "create_detector", lambda c: MovingDetector())
    monkeypatch.setattr(service_mod, "MqttBus", FakeBus)

    svc = VisionService(cfg)
    svc.setup()
    try:
        assert svc.ctrl.state is State.PAUSED, "a recovered batch must not resume on its own"
        assert svc.ctrl.batch.batch_id == "b-open"
        assert svc.ctrl.batch.count == 137
        assert svc.ctrl.batch.uncertain is True
        assert svc.io_runner.backend.run_permit is False
        assert svc.writer.flush(timeout=5)
        assert svc.store.get_batch("b-open")["recovered"] is True
        assert svc.store.get_batch("b-older")["status"] == "aborted"
        assert ("batch_recovered", ) in [(k,) for k, _ in svc.bus.events]
    finally:
        svc.shutdown()


def test_counts_with_no_batch_open_are_flagged_not_silently_dropped(running):
    svc, source, detector = running
    assert wait_until(lambda: svc._frames > 5)
    detector.objects = 1                                  # belt moving with nothing selected
    assert wait_until(lambda: svc.ctrl.unassigned_count > 0, timeout=8)
    assert svc.ctrl.batch is None
    detector.objects = 0
    assert any(k == "unassigned_counts" for k, _ in svc.bus.events)
