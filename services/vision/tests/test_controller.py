"""
services/vision/tests/test_controller.py — checkpoint 3

Every rule that decides whether the conveyor may run, tested with a fake clock.
"""
import threading

import pytest

from vision.batch.controller import Controller, State
from vision.config import CratesCfg, SafetyCfg
from vision.io.runner import IoRunner
from vision.io.sim import SimIo

SAFETY = SafetyCfg(frame_timeout_ms=1000, max_frame_gap=2, min_fps=10, min_fps_window_s=3,
                   clear_time_s=5, heartbeat_period_ms=250)
CRATES = CratesCfg(count=12, default_target=400, max_target=2000)


class Rig:
    """Controller + fake clock + a camera feeding frames at `fps`."""

    def __init__(self, fps=30.0, flash=True):
        self.t = 1000.0
        self.fps = fps
        self.frame_id = 0
        self.events = []
        self.c = Controller(CRATES, SAFETY, alarm_flash_on_fault=flash, wallclock=lambda: 1.7e9 + self.t,
                            on_event=lambda n, d: self.events.append((n, d)))
        self.c.on_buttons(start=False, stop=False, now=self.t)  # first IO sample

    def run(self, seconds, counts_per_s=0.0, frames=True, start=False, stop=False):
        """Advance time; deliver frames, counts and button levels like the real loops would."""
        dt = 1.0 / self.fps
        steps = int(round(seconds / dt))
        expected_total = int(round(seconds * counts_per_s))
        delivered = 0
        for i in range(steps):
            self.t += dt
            if frames:
                self.frame_id += 1
                self.c.on_frame(self.frame_id, self.t)
            due = (expected_total * (i + 1)) // steps     # exact integer spread, no float drift
            if due > delivered:
                self.c.on_counts(due - delivered, self.t)
                delivered = due
            self.c.on_buttons(start=start, stop=stop, now=self.t)
            self.c.tick(self.t)

    @property
    def permit(self):
        return self.c.outputs(self.t)[0]

    @property
    def alarm(self):
        return self.c.outputs(self.t)[1]

    def names(self):
        return [n for n, _ in self.events]


def started_rig(target=100, **kw):
    r = Rig(**kw)
    r.run(0.5)
    assert r.c.select_batch(crate=3, target=target)[0]
    assert r.c.request_start(r.t, "ui")[0]
    return r


# ─── normal cycle ───────────────────────────────────────────────────────────
def test_full_batch_cycle_with_overfill():
    r = started_rig(target=100)
    assert r.c.state == State.RUNNING and r.permit and not r.alarm
    r.run(25, counts_per_s=4)                      # 100 counts in 25 s
    assert r.c.state == State.CLEARING
    assert not r.permit and r.alarm                # permit dropped at the target
    r.run(1, counts_per_s=3)                       # belt clearing: 3 more coconuts fall in
    r.run(SAFETY.clear_time_s + 0.2)               # then nothing for clear_time_s
    assert r.c.state == State.IDLE
    b = r.c.last_batch
    assert b.status == "completed" and b.count == 103 and b.overfill == 3 and not b.uncertain
    assert "target" in b.stops
    assert "batch_completed" in r.names()


def test_permit_drops_on_the_exact_count():
    r = started_rig(target=10)
    for i in range(9):
        r.c.on_counts(1, r.t)
        assert r.permit, f"permit dropped early at {i + 1}"
    r.c.on_counts(1, r.t)
    assert not r.permit and r.c.state == State.CLEARING


def test_multiple_counts_in_one_frame_cross_target():
    r = started_rig(target=10)
    r.c.on_counts(8, r.t)
    r.c.on_counts(3, r.t)                          # 11 in one frame
    assert r.c.state == State.CLEARING and r.c.batch.count == 11


# ─── start / stop rules ─────────────────────────────────────────────────────
def test_start_refused_without_batch():
    r = Rig()
    r.run(0.5)
    ok, why = r.c.request_start(r.t, "button")
    assert not ok and "select" in why
    assert not r.permit


def test_start_button_edge_only_and_held_at_boot_ignored():
    r = Rig()
    r.c = Controller(CRATES, SAFETY, wallclock=lambda: 0)   # fresh controller, button already held
    r.c.on_buttons(start=True, stop=False, now=r.t)          # first sample: held at boot
    r.run(0.5, start=True)
    r.c.select_batch(1, 50)
    r.run(0.5, start=True)                                   # still held: no new edge
    assert r.c.state == State.READY
    r.run(0.2, start=False)
    r.run(0.1, start=True)                                   # real press
    assert r.c.state == State.RUNNING and r.permit


def test_stop_button_pauses_and_blocks_start_while_held():
    r = started_rig(target=100)
    r.run(1, counts_per_s=4)
    r.run(0.1, stop=True)
    assert r.c.state == State.PAUSED and not r.permit and r.alarm
    ok, why = r.c.request_start(r.t, "ui")
    assert not ok and "stop" in why
    r.run(0.2, stop=False)
    assert r.c.request_start(r.t, "ui")[0] and r.permit
    assert "stop_button" in r.c.batch.stops


def test_counts_while_paused_go_to_batch():
    r = started_rig(target=100)
    r.c.request_stop(r.t, "ui")
    r.run(2, counts_per_s=2)
    assert r.c.batch.count == 4


def test_pause_then_target_reached_goes_to_clearing_and_cannot_restart():
    r = started_rig(target=5)
    r.c.on_counts(4, r.t)
    r.c.request_stop(r.t, "ui")
    r.c.on_counts(1, r.t)                          # the last coconut arrives while paused
    assert r.c.state == State.CLEARING
    assert not r.c.request_start(r.t, "ui")[0]


def test_select_refused_while_running():
    r = started_rig()
    assert not r.c.select_batch(4, 100)[0]


def test_cancel_ready_and_paused_batches():
    r = Rig()
    r.run(0.5)
    r.c.select_batch(2, 100)
    assert r.c.cancel_batch()[0] and r.c.state == State.IDLE
    assert r.c.last_batch.status == "aborted"


def test_invalid_crate_and_target():
    r = Rig()
    assert not r.c.select_batch(13, 100)[0]
    assert not r.c.select_batch(1, 0)[0]
    assert not r.c.select_batch(1, 5000)[0]


# ─── faults ─────────────────────────────────────────────────────────────────
def test_camera_stops_while_running_faults_within_timeout():
    r = started_rig()
    r.run(0.95, frames=False)
    assert r.permit                                # still inside frame_timeout_ms
    r.run(0.1, frames=False)
    assert r.c.state == State.FAULT and not r.permit
    assert r.c.batch.uncertain and "frame_timeout" in r.c.batch.faults


def test_fault_reset_refused_until_camera_back_then_needs_new_start():
    r = started_rig()
    r.run(1.5, frames=False)
    assert not r.c.reset_fault(r.t)[0]
    r.run(0.5)                                     # frames flowing again
    assert r.c.reset_fault(r.t)[0]
    assert r.c.state == State.PAUSED and not r.permit   # never auto-restarts
    assert r.c.request_start(r.t, "ui")[0]


def test_frame_gap_faults():
    r = started_rig()
    r.frame_id += 5                                # 5 frames lost at once
    r.run(0.1)
    assert r.c.state == State.FAULT and r.c.batch.frames_lost >= 5


def test_single_lost_frame_is_recorded_not_fatal():
    r = started_rig()
    r.frame_id += 1
    r.run(0.1)
    assert r.c.state == State.RUNNING and r.c.batch.frames_lost == 1


def test_low_fps_faults_after_window():
    r = started_rig(fps=6)                         # below min_fps 10
    r.run(SAFETY.min_fps_window_s - 0.5)
    assert r.c.state == State.RUNNING              # not judged before a full window
    r.run(1.0)
    assert r.c.state == State.FAULT and any(x.startswith("low_fps") for x in r.c.fault_reasons)


def test_dead_camera_blocks_start_from_idle():
    r = Rig()
    r.run(0.5)
    r.run(1.5, frames=False)
    assert r.c.state == State.FAULT
    assert not r.c.select_batch(1, 100)[0]


def test_counts_with_no_batch_are_reported():
    r = Rig()
    r.run(0.5)
    r.c.on_counts(2, r.t)
    assert r.c.unassigned_count == 2 and "unassigned_counts" in r.names()


def test_alarm_flashes_in_fault_and_is_steady_when_stopped():
    r = started_rig()
    r.run(1.5, frames=False)
    samples = set()
    for _ in range(20):
        r.t += 0.1
        samples.add(r.c.outputs(r.t)[1])
    assert samples == {True, False}                # flashing
    r2 = Rig(flash=False)
    r2.run(1.5, frames=False)
    assert r2.alarm is True


# ─── IO thread (sim backend) ────────────────────────────────────────────────
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def io_rig():
    clock = FakeClock()
    io = SimIo()
    ctrl = Controller(CRATES, SAFETY, wallclock=lambda: 0)
    runner = IoRunner(io, ctrl, threading.Lock(), debounce_ms=30, period_s=0.02, clock=clock)
    return clock, io, ctrl, runner


def step(clock, runner, ctrl, seconds, frames=True):
    for _ in range(int(round(seconds / 0.02))):
        clock.t += 0.02
        if frames:
            ctrl.on_frame(int(clock.t * 50), clock.t)
        runner.cycle()


def test_io_thread_drives_relay_from_buttons():
    clock, io, ctrl, runner = io_rig()
    step(clock, runner, ctrl, 0.2)
    ctrl.select_batch(1, 10)
    io.set_start(True)
    step(clock, runner, ctrl, 0.1)
    io.set_start(False)
    step(clock, runner, ctrl, 0.1)
    assert io.run_permit and not io.alarm_light
    io.set_stop(True)
    step(clock, runner, ctrl, 0.1)
    assert not io.run_permit and io.alarm_light and ctrl.state == State.PAUSED


def test_debounce_ignores_glitch():
    clock, io, ctrl, runner = io_rig()
    step(clock, runner, ctrl, 0.2)
    ctrl.select_batch(1, 10)
    io.set_start(True)
    step(clock, runner, ctrl, 0.02)                # 20 ms blip < 30 ms debounce
    io.set_start(False)
    step(clock, runner, ctrl, 0.2)
    assert ctrl.state == State.READY and not io.run_permit


def test_io_read_failure_is_fail_safe():
    clock, io, ctrl, runner = io_rig()
    step(clock, runner, ctrl, 0.2)
    ctrl.select_batch(1, 10)
    ctrl.request_start(clock.t)
    step(clock, runner, ctrl, 0.1)
    assert io.run_permit
    io.fail_reads = True
    step(clock, runner, ctrl, 0.02)
    assert not io.run_permit                       # first failed read: stop treated as pressed
    step(clock, runner, ctrl, 0.2)
    assert ctrl.state == State.FAULT and "io_error" in ctrl.fault_reasons


def test_stalled_vision_loop_drops_permit_via_io_thread():
    clock, io, ctrl, runner = io_rig()
    step(clock, runner, ctrl, 0.2)
    ctrl.select_batch(1, 10)
    ctrl.request_start(clock.t)
    step(clock, runner, ctrl, 0.1)
    assert io.run_permit
    step(clock, runner, ctrl, 1.1, frames=False)   # vision loop hung: no frames, IO thread still cycling
    assert not io.run_permit and ctrl.state == State.FAULT


def test_safe_state_on_stop():
    _, io, ctrl, runner = io_rig()
    io.run_permit = True
    runner.stop()
    assert not io.run_permit and io.alarm_light
