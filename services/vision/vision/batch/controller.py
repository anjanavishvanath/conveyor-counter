"""
services/vision/vision/batch/controller.py

The conveyor/batch state machine. It decides when the run-permit relay may be energized.
It is pure logic: no threads, no hardware and no real clock (time is passed in), so every
behaviour below is covered by unit tests with a fake clock.

States
  IDLE      no crate selected. Permit OFF. Start is refused (coconuts would go uncounted).
  READY     crate + target selected, waiting for START (button or UI). Permit OFF.
  RUNNING   permit ON. Counts are added to the batch.
  PAUSED    STOP pressed (button or UI) before target. Permit OFF. Counting continues
            (the flat belt still carries coconuts into the open crate). START resumes.
  CLEARING  target reached: permit OFF, counting continues until no new count for clear_time_s,
            then the batch is COMPLETE (final count = target + overfill) and the state returns to IDLE.
  FAULT     camera/frames/processing/IO problem. Permit OFF, alarm flashing. The batch is marked
            UNCERTAIN. Leaving FAULT needs an explicit reset_fault once the cause has gone,
            and then a new START. Nothing ever restarts the conveyor by itself.

Permit rule: permit is ON only in RUNNING, and only while the stop button is not active.
Alarm rule:  alarm ON whenever permit is OFF; flashing (1 Hz) in FAULT if configured.
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Callable, Deque, Dict, List, Optional, Tuple

from vision.config import CratesCfg, SafetyCfg


class State(str, Enum):
    IDLE = "IDLE"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CLEARING = "CLEARING"
    FAULT = "FAULT"


@dataclass
class Batch:
    batch_id: str
    crate: int
    target: int
    count: int = 0
    status: str = "open"                   # open | completed | aborted
    created_at: float = 0.0                # wall-clock epoch seconds (UTC)
    started_at: Optional[float] = None
    target_reached_at: Optional[float] = None
    completed_at: Optional[float] = None
    uncertain: bool = False                # a fault happened during this batch: review the count
    frames_lost: int = 0
    faults: List[str] = field(default_factory=list)
    stops: List[str] = field(default_factory=list)   # why the permit dropped: stop_button, ui_stop, target, fault:...

    @property
    def overfill(self) -> int:
        return max(0, self.count - self.target)

    @classmethod
    def from_row(cls, row: Dict) -> "Batch":
        """Rebuild a batch from a stored row (used when recovering after a restart)."""
        return cls(
            batch_id=row["batch_id"],
            crate=int(row["crate"]),
            target=int(row["target"]),
            count=int(row.get("count") or 0),
            status=row.get("status", "open"),
            created_at=float(row.get("created_at") or 0.0),
            started_at=row.get("started_at"),
            target_reached_at=row.get("target_reached_at"),
            completed_at=row.get("completed_at"),
            uncertain=bool(row.get("uncertain")),
            frames_lost=int(row.get("frames_lost") or 0),
            faults=list(row.get("faults") or []),
            stops=list(row.get("stops") or []),
        )


Event = Tuple[str, Dict]


class Controller:
    FLASH_PERIOD_S = 1.0

    def __init__(self, crates: CratesCfg, safety: SafetyCfg, alarm_flash_on_fault: bool = True,
                 wallclock: Callable[[], float] = time.time,
                 on_event: Optional[Callable[[str, Dict], None]] = None):
        self.crates = crates
        self.safety = safety
        self.alarm_flash = alarm_flash_on_fault
        self.wallclock = wallclock
        self.on_event = on_event or (lambda name, data: None)

        self.state = State.IDLE
        self.batch: Optional[Batch] = None
        self.last_batch: Optional[Batch] = None
        self.fault_reasons: List[str] = []
        self.unassigned_count = 0            # coconuts counted with no batch open (should stay 0)

        self._stop_active = False
        self._start_prev: Optional[bool] = None   # None = first sample not yet seen (held-at-boot guard)
        self._last_frame_t: Optional[float] = None
        self._last_frame_id: Optional[int] = None
        self._frame_times: Deque[float] = deque()
        self._running_since: Optional[float] = None
        self._last_count_t: Optional[float] = None
        self._clearing_since: Optional[float] = None
        self._fault_t: float = 0.0

    # ───────────────────────── helpers ─────────────────────────
    def _emit(self, name: str, **data) -> None:
        data.setdefault("state", self.state.value)
        if self.batch:
            data.setdefault("batch_id", self.batch.batch_id)
        self.on_event(name, data)

    def _set_state(self, new: State, reason: str = "") -> None:
        if new != self.state:
            old = self.state
            self.state = new
            self._emit("state_changed", old=old.value, new=new.value, reason=reason)

    def _drop_permit(self, why: str) -> None:
        if self.batch is not None:
            self.batch.stops.append(why)

    # ───────────────────────── commands (API / UI) ─────────────────────────
    def select_batch(self, crate: int, target: int) -> Tuple[bool, str]:
        if self.state not in (State.IDLE, State.READY):
            return self._reject("select_batch", f"not allowed in {self.state.value}")
        if not 1 <= crate <= self.crates.count:
            return self._reject("select_batch", f"crate must be 1..{self.crates.count}")
        if not 1 <= target <= self.crates.max_target:
            return self._reject("select_batch", f"target must be 1..{self.crates.max_target}")
        if self.batch is not None and self.state == State.READY:
            self.batch.status = "aborted"          # replacing a READY batch that never started
            self._emit("batch_aborted", batch=asdict(self.batch))
        self.batch = Batch(batch_id=str(uuid.uuid4()), crate=crate, target=target, created_at=self.wallclock())
        self._set_state(State.READY, "batch selected")
        self._emit("batch_selected", batch=asdict(self.batch))
        return True, "ok"

    def cancel_batch(self) -> Tuple[bool, str]:
        if self.state not in (State.READY, State.PAUSED):
            return self._reject("cancel_batch", f"not allowed in {self.state.value}")
        self._close_batch("aborted")
        self._set_state(State.IDLE, "batch cancelled")
        return True, "ok"

    def request_start(self, now: float, source: str = "ui") -> Tuple[bool, str]:
        if self.state == State.FAULT:
            return self._reject("start", "fault active: reset the fault first")
        if self.state not in (State.READY, State.PAUSED):
            reason = "select a crate and target first" if self.state == State.IDLE else f"not allowed in {self.state.value}"
            return self._reject("start", reason)
        if self._stop_active:
            return self._reject("start", "stop button is active")
        if not self._camera_ok(now):
            return self._reject("start", "no camera frames")
        if self.batch.count >= self.batch.target:
            return self._reject("start", "target already reached")
        if self.batch.started_at is None:
            self.batch.started_at = self.wallclock()
        self._running_since = now
        self._set_state(State.RUNNING, f"start ({source})")
        return True, "ok"

    def request_stop(self, now: float, source: str = "ui") -> Tuple[bool, str]:
        if self.state == State.RUNNING:
            self._drop_permit(f"{source}_stop")
            self._set_state(State.PAUSED, f"stop ({source})")
        return True, "ok"   # stop is always accepted

    def finish_batch(self, now: float) -> Tuple[bool, str]:
        """Operator confirms the belt is clear before clear_time_s has elapsed."""
        if self.state != State.CLEARING:
            return self._reject("finish_batch", f"not allowed in {self.state.value}")
        self._complete(now)
        return True, "ok"

    def reset_fault(self, now: float) -> Tuple[bool, str]:
        if self.state != State.FAULT:
            return self._reject("reset_fault", "no fault active")
        if not self._camera_ok(now):
            return self._reject("reset_fault", "camera frames still missing")
        cleared = self.fault_reasons
        self.fault_reasons = []
        if self.batch is None:
            self._set_state(State.IDLE, "fault reset")
        elif self.batch.target_reached_at is not None:
            self._clearing_since = now
            self._last_count_t = now
            self._set_state(State.CLEARING, "fault reset")
        elif self.batch.started_at is None:
            self._set_state(State.READY, "fault reset")
        else:
            self._set_state(State.PAUSED, "fault reset")
        self._emit("fault_cleared", reasons=cleared)
        return True, "ok"

    def restore_batch(self, batch: Batch, now: float) -> Tuple[bool, str]:
        """
        Re-adopt a batch that was still open when the service stopped (power loss, crash,
        software restart).

        The batch is always marked UNCERTAIN: while the service was down nothing was
        counting, so coconuts already on the belt may have reached the crate unrecorded.
        The operator must confirm or correct the number.

        It never resumes by itself. The conveyor only moves again after an explicit START,
        exactly as after any other stop.
        """
        if self.state != State.IDLE or self.batch is not None:
            return self._reject("restore_batch", f"not allowed in {self.state.value}")
        self.batch = batch
        batch.status = "open"
        batch.uncertain = True
        if "restart_during_batch" not in batch.faults:
            batch.faults.append("restart_during_batch")
        batch.stops.append("service_restart")

        if batch.count >= batch.target:
            # The target was already reached before the restart: let it finish clearing
            # so the final number gets written, instead of leaving the row open forever.
            if batch.target_reached_at is None:
                batch.target_reached_at = self.wallclock()
            self._clearing_since = now
            self._last_count_t = now
            self._set_state(State.CLEARING, "recovered after restart")
        elif batch.started_at is None:
            self._set_state(State.READY, "recovered after restart")
        else:
            self._set_state(State.PAUSED, "recovered after restart")
        self._emit("batch_recovered", batch=asdict(batch))
        return True, "ok"

    def _reject(self, command: str, reason: str) -> Tuple[bool, str]:
        self._emit("command_rejected", command=command, reason=reason)
        return False, reason

    # ───────────────────────── inputs from the vision loop ─────────────────────────
    def on_frame(self, frame_id: int, now: float) -> None:
        if self._last_frame_id is not None:
            gap = frame_id - self._last_frame_id
            if gap > 1:
                lost = gap - 1
                if self.batch is not None and self.state in (State.RUNNING, State.PAUSED, State.CLEARING):
                    self.batch.frames_lost += lost
                if gap > self.safety.max_frame_gap and self.state in (State.RUNNING, State.PAUSED, State.CLEARING):
                    self.fault(now, f"frames_lost:{lost}")
        self._last_frame_id = frame_id
        self._last_frame_t = now
        self._frame_times.append(now)
        while self._frame_times and now - self._frame_times[0] > self.safety.min_fps_window_s:
            self._frame_times.popleft()

    def on_counts(self, n: int, now: float) -> None:
        if n <= 0:
            return
        self._last_count_t = now
        if self.batch is not None and self.state in (State.RUNNING, State.PAUSED, State.CLEARING, State.FAULT):
            self.batch.count += n
            self._emit("count", count=self.batch.count, added=n)
            if self.batch.count >= self.batch.target and self.batch.target_reached_at is None:
                self.batch.target_reached_at = self.wallclock()
                if self.state == State.RUNNING:
                    self._drop_permit("target")
                self._clearing_since = now
                if self.state != State.FAULT:
                    self._set_state(State.CLEARING, "target reached")
                self._emit("target_reached", count=self.batch.count, target=self.batch.target)
        else:
            # Coconuts moving with no batch open (READY/IDLE): nobody knows which crate they went to.
            self.unassigned_count += n
            self._emit("unassigned_counts", added=n, total_unassigned=self.unassigned_count)

    def on_buttons(self, start: bool, stop: bool, now: float) -> None:
        """Debounced button states. Stop acts on level, start acts on the rising edge only."""
        if stop and not self._stop_active:
            self._emit("stop_button", active=True)
            if self.state == State.RUNNING:
                self._drop_permit("stop_button")
                self._set_state(State.PAUSED, "stop button")
        elif not stop and self._stop_active:
            self._emit("stop_button", active=False)
        self._stop_active = stop

        if self._start_prev is None:
            self._start_prev = start      # a button held at boot is not a press
        elif start and not self._start_prev:
            self.request_start(now, source="button")
        self._start_prev = start

    def fault(self, now: float, reason: str) -> None:
        if reason not in self.fault_reasons:
            self.fault_reasons.append(reason)
        if self.batch is not None:
            self.batch.uncertain = True
            self.batch.faults.append(reason)
            if self.state == State.RUNNING:
                self._drop_permit(f"fault:{reason}")
        if self.state != State.FAULT:
            self._fault_t = now
            self._set_state(State.FAULT, reason)
        self._emit("fault", reason=reason, reasons=list(self.fault_reasons))

    # ───────────────────────── periodic ─────────────────────────
    def _camera_ok(self, now: float) -> bool:
        return self._last_frame_t is not None and (now - self._last_frame_t) * 1000 <= self.safety.frame_timeout_ms

    def tick(self, now: float) -> None:
        """Call often (the IO thread calls it at ~50 Hz). Detects stalls and finishes clearing."""
        # 1. Frame timeout: camera or vision loop stalled. Checked in every state, so a dead camera
        #    also blocks starting from IDLE/READY (FAULT) instead of failing silently later.
        if self._last_frame_t is not None and not self._camera_ok(now) and self.state != State.FAULT:
            self.fault(now, "frame_timeout")
        if self._last_frame_t is None and self.state == State.RUNNING:
            self.fault(now, "frame_timeout")

        # 2. Processing too slow while the conveyor runs (only after a full window of running)
        if (self.state == State.RUNNING and self._running_since is not None
                and now - self._running_since >= self.safety.min_fps_window_s):
            fps = len(self._frame_times) / self.safety.min_fps_window_s
            if fps < self.safety.min_fps:
                self.fault(now, f"low_fps:{fps:.1f}")

        # 3. Clearing finished: no new count for clear_time_s
        if self.state == State.CLEARING:
            last = max(self._last_count_t or 0.0, self._clearing_since or 0.0)
            if now - last >= self.safety.clear_time_s:
                self._complete(now)

    def _complete(self, now: float) -> None:
        self._close_batch("completed")
        self._set_state(State.IDLE, "batch complete")

    def _close_batch(self, status: str) -> None:
        b = self.batch
        if b is None:
            return
        b.status = status
        b.completed_at = self.wallclock()
        self._emit("batch_completed" if status == "completed" else "batch_aborted", batch=asdict(b))
        self.last_batch, self.batch = b, None

    # ───────────────────────── outputs ─────────────────────────
    def outputs(self, now: float) -> Tuple[bool, bool]:
        """(run_permit, alarm_light) to write to the field IO right now."""
        permit = self.state == State.RUNNING and not self._stop_active
        if permit:
            return True, False
        if self.state == State.FAULT and self.alarm_flash:
            phase = ((now - self._fault_t) % self.FLASH_PERIOD_S) < self.FLASH_PERIOD_S / 2
            return False, phase
        return False, True

    def snapshot(self, now: float) -> Dict:
        permit, alarm = self.outputs(now)
        fps = len(self._frame_times) / self.safety.min_fps_window_s if self._frame_times else 0.0
        return {
            "state": self.state.value,
            "run_permit": permit,
            "alarm_light": alarm,
            "stop_button_active": self._stop_active,
            "camera_ok": self._camera_ok(now),
            "fps": round(fps, 1),
            "fault_reasons": list(self.fault_reasons),
            "unassigned_count": self.unassigned_count,
            "batch": asdict(self.batch) if self.batch else None,
            "last_batch": asdict(self.last_batch) if self.last_batch else None,
        }
