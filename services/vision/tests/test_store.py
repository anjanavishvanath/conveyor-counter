"""
services/vision/tests/test_store.py — persistence, production-day reporting and
power-loss recovery (checkpoint 4).

What matters here is that a batch's count survives the process dying, and that a recovered
batch is never silently trusted or silently restarted.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from vision.batch.controller import Batch, Controller, State
from vision.config import CratesCfg, SafetyCfg
from vision.store.db import (Store, StoreWriter, production_day_bounds, production_day_of)

TZ = "Asia/Colombo"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "counter.db", synchronous="NORMAL")
    s.migrate()
    yield s
    s.close()


def _controller(clock, **safety):
    cfg = SafetyCfg(clear_time_s=5.0, min_fps=10.0, **safety)
    return Controller(CratesCfg(count=12, default_target=400, max_target=2000), cfg,
                      wallclock=lambda: 1_760_000_000.0)


def _extra():
    return {"site_name": "plant", "product": "coconut", "detector": "onnx_cpu/cpu",
            "model_file": "best.onnx"}


# ───────────────────────── schema and basic writes ─────────────────────────
def test_migrate_is_idempotent(store, tmp_path):
    store.migrate()
    store.migrate()
    assert store.open_batch() is None


def test_batch_roundtrip(store):
    b = Batch(batch_id="b1", crate=3, target=400, created_at=1000.0)
    store.save_batch(asdict(b), **_extra())
    row = store.get_batch("b1")
    assert (row["crate"], row["target"], row["status"], row["count"]) == (3, 400, "open", 0)
    assert row["product"] == "coconut" and row["faults"] == [] and row["uncertain"] is False

    b.count, b.status, b.completed_at, b.uncertain = 402, "completed", 1100.0, True
    b.faults.append("low_fps:8.1")
    b.stops += ["target", "ui_stop"]
    store.save_batch(asdict(b), **_extra())
    row = store.get_batch("b1")
    assert (row["count"], row["status"], row["overfill"]) == (402, "completed", 2)
    assert row["faults"] == ["low_fps:8.1"] and row["stops"] == ["target", "ui_stop"]
    assert row["uncertain"] is True


def test_counts_are_idempotent_and_never_go_backwards(store):
    store.save_batch(asdict(Batch(batch_id="b1", crate=1, target=10, created_at=0.0)), **_extra())
    for n in range(1, 6):
        store.save_count("b1", n, 1, 100.0 + n)
    store.save_count("b1", 3, 1, 999.0)          # replayed event
    store.save_count("b1", 2, 1, 999.0)          # stale event arriving late
    assert store.get_batch("b1")["count"] == 5
    rows = store._conn().execute("SELECT COUNT(*) c FROM counts WHERE batch_id='b1'").fetchone()
    assert rows["c"] == 5


def test_corrections_do_not_touch_the_machine_count(store):
    store.save_batch(asdict(Batch(batch_id="b1", crate=1, target=400, count=398,
                                  status="completed", created_at=0.0)), **_extra())
    store.add_correction("b1", delta=2, reason="2 rejected nuts removed", author="supervisor", ts=50.0)
    assert store.get_batch("b1")["count"] == 398          # original is untouched
    assert sum(c["delta"] for c in store.corrections_for("b1")) == 2


# ───────────────────────── production day ─────────────────────────
def test_production_day_bounds_span_24h():
    start, end = production_day_bounds(date(2026, 10, 18), TZ, "06:00")
    assert end - start == 86_400
    assert datetime.fromtimestamp(start, ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M") == "2026-10-18 06:00"


def test_night_shift_hours_belong_to_the_previous_day():
    tz = ZoneInfo(TZ)
    before = datetime(2026, 10, 19, 5, 30, tzinfo=tz).timestamp()
    after = datetime(2026, 10, 19, 6, 30, tzinfo=tz).timestamp()
    assert production_day_of(before, TZ, "06:00") == date(2026, 10, 18)
    assert production_day_of(after, TZ, "06:00") == date(2026, 10, 19)
    # with the default midnight boundary both fall on the 19th
    assert production_day_of(before, TZ, "00:00") == date(2026, 10, 19)


def test_day_summary_counts_only_completed_batches(store):
    start, _ = production_day_bounds(date(2026, 10, 18), TZ, "06:00")
    for i, (crate, count, status) in enumerate([(1, 400, "completed"), (1, 402, "completed"),
                                                (2, 150, "aborted"), (3, 399, "completed")]):
        store.save_batch(asdict(Batch(batch_id=f"b{i}", crate=crate, target=400, count=count,
                                      status=status, created_at=start + 3600 + i)), **_extra())
    s = store.day_summary(date(2026, 10, 18), TZ, "06:00")
    assert s["batches"] == 3 and s["counted"] == 1201
    assert {r["crate"]: r["counted"] for r in s["per_crate"]} == {1: 802, 3: 399}


def test_csv_export_includes_corrected_count(store):
    start, end = production_day_bounds(date(2026, 10, 18), TZ, "00:00")
    store.save_batch(asdict(Batch(batch_id="b1", crate=5, target=400, count=401, status="completed",
                                  created_at=start + 10, started_at=start + 20,
                                  completed_at=start + 500)), **_extra())
    store.add_correction("b1", delta=-1, reason="one split nut", ts=start + 600)
    rows = list(store.export_rows(start, end, TZ, "00:00"))
    assert len(rows) == 1
    row = dict(zip(Store.CSV_HEADER, rows[0]))
    assert row["crate"] == 5 and row["count"] == 401 and row["overfill"] == 1
    assert row["corrections"] == -1 and row["corrected_count"] == 400
    assert row["production_day"] == "2026-10-18" and row["duration_s"] == 480.0


# ───────────────────────── asynchronous writer ─────────────────────────
def test_writer_persists_without_blocking_the_caller(store):
    w = StoreWriter(store)
    w.start()
    try:
        w.save_batch(asdict(Batch(batch_id="b1", crate=1, target=5, created_at=0.0)), **_extra())
        for n in range(1, 6):
            w.save_count("b1", n, 1, float(n))
        w.log_event("fault", 10.0, "b1", {"reason": "low_fps:8.0"})
        assert w.flush(timeout=5.0)
    finally:
        w.stop()
    assert store.get_batch("b1")["count"] == 5
    assert [e["kind"] for e in store.events_for("b1")] == ["fault"]


def test_writer_reports_persistent_failures(store, monkeypatch):
    errors = []
    w = StoreWriter(store, on_error=errors.append, max_errors=2)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_count", boom)
    w.start()
    try:
        for n in range(1, 4):
            w.save_count("b1", n, 1, float(n))
        w.flush(timeout=5.0)
    finally:
        w.stop()
    assert errors, "a database that keeps failing must be reported so the service can FAULT"


# ───────────────────────── recovery after a restart ─────────────────────────
def test_open_batch_is_recovered_as_paused_and_uncertain(store):
    now = [100.0]
    ctrl = _controller(now)
    ctrl.select_batch(crate=7, target=400)
    ctrl.request_start(now[0])
    ctrl.on_frame(1, now[0])                       # camera alive so start is allowed
    ctrl.request_start(now[0])
    ctrl.on_counts(120, now[0])
    store.save_batch(asdict(ctrl.batch), **_extra())

    # --- power loss here: the row stays 'open' ---
    row = store.open_batch()
    assert row is not None and row["count"] == 120

    fresh = _controller(now)
    ok, _ = fresh.restore_batch(Batch.from_row(row), now=200.0)
    assert ok
    assert fresh.state is State.PAUSED             # never resumes on its own
    assert fresh.batch.count == 120
    assert fresh.batch.uncertain is True
    assert "restart_during_batch" in fresh.batch.faults
    assert fresh.outputs(200.0)[0] is False        # run permit stays OFF

    store.mark_recovered(row["batch_id"])
    assert store.get_batch(row["batch_id"])["recovered"] is True


def test_recovered_batch_past_its_target_finishes_clearing(store):
    now = 500.0
    row = asdict(Batch(batch_id="b1", crate=2, target=400, count=405, created_at=0.0,
                       started_at=10.0, target_reached_at=90.0))
    ctrl = _controller(now)
    ok, _ = ctrl.restore_batch(Batch.from_row(row), now=now)
    assert ok and ctrl.state is State.CLEARING
    ctrl.tick(now + 6.0)                           # clear_time_s = 5
    assert ctrl.state is State.IDLE
    assert ctrl.last_batch.status == "completed" and ctrl.last_batch.uncertain is True


def test_restore_is_refused_when_a_batch_is_already_open():
    ctrl = _controller(0.0)
    ctrl.select_batch(crate=1, target=10)
    ok, reason = ctrl.restore_batch(Batch(batch_id="x", crate=2, target=10), now=1.0)
    assert not ok and "not allowed" in reason


def test_stale_open_rows_are_closed_on_recovery(store):
    for i in range(3):
        store.save_batch(asdict(Batch(batch_id=f"b{i}", crate=1, target=400, count=10 * i,
                                      created_at=1000.0 + i)), **_extra())
    newest = store.open_batch()
    assert newest["batch_id"] == "b2"
    assert store.abort_open_batches(except_id="b2") == 2
    assert store.get_batch("b0")["status"] == "aborted"
    assert store.get_batch("b2")["status"] == "open"
