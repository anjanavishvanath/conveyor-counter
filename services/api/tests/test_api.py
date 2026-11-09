"""
services/api/tests/test_api.py

API tests with a fake bus (no broker, no vision service) and a vision database written through
the vision package's own Store, exactly as the vision service writes it.

    cd services/api
    python -m pytest -q
"""
from __future__ import annotations

import csv
import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.bus import BusUnavailable, CommandTimeout, LiveBus
from api.settings import load_config
from vision.store.db import Store

SITE = """
site_name: "Test plant"
timezone: "Asia/Colombo"
product_profile: "profile.yaml"
crates: {count: 12, default_target: 400, max_target: 2000}
frame_source: {type: video_file, video_file: {path: "x.mp4"}}
storage: {db_path: "data/counter.db"}
api:
  settings_db: "data/api.db"
  export_dir: "data/exports"
  usb_mount_roots: ["no-such-dir"]
  ui_dist: "no-ui"
  allow_system_control: {allow}
"""

PROFILE = """
product: testprod
model: {file: "m.onnx", class_names: ["thing"]}
"""


class FakeBus(LiveBus):
    """LiveBus without a network: commands are answered by `self.answer(name, payload)`."""

    def __init__(self):
        super().__init__("localhost", 1883, "counter")
        self.sent = []
        self.answer = lambda name, payload: {"ok": True, "reason": "ok"}
        self.mode = "ok"            # ok | down | timeout

    def start(self):
        self._connected.set()

    def stop(self):
        pass

    async def command(self, name, payload, timeout_s):
        if self.mode == "down":
            raise BusUnavailable("message bus (MQTT broker) is not connected")
        if self.mode == "timeout":
            raise CommandTimeout(f"vision service did not answer '{name}'")
        self.sent.append((name, payload))
        return self.answer(name, payload)

    def set_state(self, state: str):
        self.state = {"state": state}
        self.state_rx = time.monotonic()
        self.state_live = True


def make_cfg(tmp_path: Path, allow: bool = False):
    (tmp_path / "profile.yaml").write_text(PROFILE)
    (tmp_path / "site.yaml").write_text(SITE.replace("{allow}", "true" if allow else "false"))
    return load_config(tmp_path / "site.yaml")


@pytest.fixture
def env(tmp_path):
    cfg = make_cfg(tmp_path)
    bus = FakeBus()
    with TestClient(create_app(cfg, bus=bus)) as client:
        yield client, bus, cfg


def batch_row(crate, target, count, created, status="completed", uncertain=False):
    return {"batch_id": f"b-{crate}-{created}", "crate": crate, "target": target, "count": count,
            "status": status, "uncertain": uncertain, "frames_lost": 0, "created_at": created,
            "started_at": created + 1, "completed_at": created + 61, "faults": [], "stops": ["target"]}


def write_vision_db(cfg, rows):
    s = Store(cfg.site.storage.db_path)
    s.migrate()
    for r in rows:
        s.save_batch(r, site_name="Test plant", product="testprod", detector="onnx_cpu/cpu", model_file="m.onnx")
    s.close()


# ───────────────────────── presets ─────────────────────────
def test_presets_default_then_set_one_and_all(env):
    client, bus, _ = env
    crates = client.get("/api/crates").json()["crates"]
    assert len(crates) == 12 and all(c["target"] == 400 for c in crates)

    r = client.put("/api/presets/3", json={"target": 350})
    assert r.status_code == 200
    assert [c["target"] for c in r.json()["crates"]][2] == 350

    r = client.put("/api/presets", json={"target": 420})
    assert all(c["target"] == 420 for c in r.json()["crates"])


def test_preset_validation(env):
    client, _, _ = env
    assert client.put("/api/presets/13", json={"target": 100}).status_code == 422
    assert client.put("/api/presets/1", json={"target": 2001}).status_code == 422
    assert client.put("/api/presets/1", json={"target": 0}).status_code == 422
    assert client.put("/api/presets", json={"target": 99999}).status_code == 422


def test_presets_survive_restart(tmp_path):
    cfg = make_cfg(tmp_path)
    with TestClient(create_app(cfg, bus=FakeBus())) as c:
        c.put("/api/presets/5", json={"target": 123})
    with TestClient(create_app(cfg, bus=FakeBus())) as c:
        assert c.get("/api/crates").json()["crates"][4]["target"] == 123


# ───────────────────────── commands ─────────────────────────
def test_select_uses_preset_and_returns_vision_answer(env):
    client, bus, _ = env
    client.put("/api/presets/7", json={"target": 380})
    r = client.post("/api/batch/select", json={"crate": 7})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert bus.sent[-1] == ("select_batch", {"crate": 7, "target": 380})

    client.post("/api/batch/select", json={"crate": 7, "target": 10})
    assert bus.sent[-1] == ("select_batch", {"crate": 7, "target": 10})


def test_rejection_reason_is_passed_through(env):
    client, bus, _ = env
    bus.answer = lambda n, p: {"ok": False, "reason": "select a crate and target first"}
    r = client.post("/api/command/start")
    assert r.status_code == 200
    assert r.json() == {"command": "start", "ok": False, "reason": "select a crate and target first"}


def test_unknown_command_and_bad_crate(env):
    client, bus, _ = env
    assert client.post("/api/command/select_batch").status_code == 404   # must go through /batch/select
    assert client.post("/api/command/launch").status_code == 404
    assert client.post("/api/batch/select", json={"crate": 13}).status_code == 422
    assert bus.sent == []


def test_bus_down_and_vision_timeout_are_reported(env):
    client, bus, _ = env
    bus.mode = "down"
    r = client.post("/api/command/stop")
    assert r.status_code == 503 and "not connected" in r.json()["detail"]
    bus.mode = "timeout"
    r = client.post("/api/command/stop")
    assert r.status_code == 504 and "did not answer" in r.json()["detail"]


# ───────────────────────── reports ─────────────────────────
def test_reports_before_vision_db_exists(env):
    client, _, _ = env
    r = client.get("/api/reports/day").json()
    assert r["db_ready"] is False and r["batches"] == []
    assert client.get("/api/reports/day.csv").status_code == 200


def test_day_report_csv_and_crate_tiles(env):
    client, _, cfg = env
    now = time.time()
    write_vision_db(cfg, [batch_row(1, 400, 403, now - 600), batch_row(2, 400, 398, now - 300, uncertain=True),
                          batch_row(1, 400, 401, now - 100), batch_row(3, 400, 120, now - 50, status="open")])
    r = client.get("/api/reports/day").json()
    assert r["db_ready"] is True
    assert r["summary"]["batches"] == 3 and r["summary"]["counted"] == 403 + 398 + 401
    assert r["summary"]["uncertain_batches"] == 1
    assert len(r["batches"]) == 4

    tiles = {c["crate"]: c for c in client.get("/api/crates").json()["crates"]}
    assert tiles[1]["last"]["count"] == 401           # the most recent finished batch of crate 1
    assert tiles[2]["last"]["uncertain"] is True
    assert tiles[3]["last"] is None                    # open batch is not "finished"

    resp = client.get("/api/reports/day.csv")
    assert resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0][0] == "batch_id" and len(rows) == 5


def test_bad_day(env):
    client, _, _ = env
    assert client.get("/api/reports/day?day=2026-13-45").status_code == 422


def test_export_falls_back_to_local_folder(env):
    client, _, cfg = env
    write_vision_db(cfg, [batch_row(4, 400, 400, time.time() - 60)])
    r = client.post("/api/reports/export", json={}).json()
    assert r["ok"] and r["where"] == "local" and r["rows"] == 1
    assert Path(r["path"]).is_file() and Path(r["path"]).parent == cfg.site.api.export_dir


# ───────────────────────── system ─────────────────────────
def test_system_control_disabled_by_default(env):
    client, _, _ = env
    r = client.post("/api/system/shutdown")
    assert r.status_code == 403


def test_system_refused_while_conveyor_busy(tmp_path):
    cfg = make_cfg(tmp_path, allow=True)
    cfg.site.api.restart_cmd = ["python", "-c", "print('ok')"]
    bus = FakeBus()
    with TestClient(create_app(cfg, bus=bus)) as client:
        bus.set_state("RUNNING")
        assert client.post("/api/system/restart").status_code == 409
        bus.set_state("IDLE")
        assert client.post("/api/system/restart").json()["ok"] is True


# ───────────────────────── websocket ─────────────────────────
def test_websocket_sends_hello_state_and_broadcasts(env):
    client, bus, _ = env
    bus.set_state("READY")
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        first = ws.receive_json()
        assert first["type"] == "state" and first["data"]["state"] == "READY" and first["live"] is True
        client.put("/api/presets/2", json={"target": 300})
        msg = ws.receive_json()
        assert msg["type"] == "crates" and msg["data"]["crates"][1]["target"] == 300
