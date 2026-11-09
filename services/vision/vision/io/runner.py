"""
services/vision/vision/io/runner.py

The IO thread. Every cycle (default 20 ms) it:
  1. reads the push buttons and debounces them,
  2. passes them to the controller,
  3. runs the controller's timing checks (frame timeout, clearing timer, low fps),
  4. writes run_permit / alarm_light.

It is deliberately separate from the camera/inference loop. If that loop hangs (camera read
blocked, NPU call stuck), this thread still runs, sees the frames stop arriving and drops the
permit within frame_timeout_ms. If THIS thread dies or the whole process/board dies, the relay
drops because nothing keeps it energized; the external watchdog in checkpoint 10 covers
a hung-but-alive process.

All controller calls happen under one lock shared with the vision loop.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from vision.batch.controller import Controller
from vision.io.base import IoBackend

log = logging.getLogger("io")


class _Debouncer:
    def __init__(self, debounce_s: float):
        self.debounce_s = debounce_s
        self.stable: Optional[bool] = None
        self._candidate: Optional[bool] = None
        self._since = 0.0

    def update(self, raw: bool, now: float) -> bool:
        if self.stable is None:
            self.stable = raw
            self._candidate = raw
            return raw
        if raw != self._candidate:
            self._candidate, self._since = raw, now
        elif raw != self.stable and now - self._since >= self.debounce_s:
            self.stable = raw
        return self.stable


class IoRunner:
    MAX_CONSECUTIVE_IO_ERRORS = 5

    def __init__(self, backend: IoBackend, controller: Controller, lock: threading.Lock,
                 debounce_ms: int = 30, period_s: float = 0.02,
                 clock: Callable[[], float] = time.monotonic):
        self.backend = backend
        self.ctrl = controller
        self.lock = lock
        self.period_s = period_s
        self.clock = clock
        self._start = _Debouncer(debounce_ms / 1000)
        self._stop = _Debouncer(debounce_ms / 1000)
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._io_errors = 0
        self.last_cycle_t = 0.0          # for the service's own liveness check

    def cycle(self) -> None:
        """One IO cycle. Public so tests can step it without a thread."""
        now = self.clock()
        try:
            raw = self.backend.read_inputs()
            self._io_errors = 0
        except Exception as e:
            self._io_errors += 1
            log.error("IO read failed (%d in a row): %s", self._io_errors, e)
            # Can't see the stop button: treat it as pressed (fail safe) and fault after a few tries
            raw = None
        with self.lock:
            if raw is None:
                self.ctrl.on_buttons(start=False, stop=True, now=now)
                if self._io_errors >= self.MAX_CONSECUTIVE_IO_ERRORS:
                    self.ctrl.fault(now, "io_error")
            else:
                start = self._start.update(raw.start, now)
                stop = self._stop.update(raw.stop, now)
                self.ctrl.on_buttons(start=start, stop=stop, now=now)
            self.ctrl.tick(now)
            permit, alarm = self.ctrl.outputs(now)
        try:
            self.backend.write_outputs(run_permit=permit, alarm_light=alarm)
        except Exception as e:
            log.error("IO write failed: %s", e)
            with self.lock:
                self.ctrl.fault(now, "io_error")
            self.backend.safe_state()
        self.last_cycle_t = now

    def _loop(self) -> None:
        next_t = time.monotonic()
        while self._running.is_set():
            try:
                self.cycle()
            except Exception:
                log.exception("IO cycle crashed; forcing safe state")
                self.backend.safe_state()
            next_t += self.period_s
            time.sleep(max(0.0, next_t - time.monotonic()))
            if time.monotonic() - next_t > 1.0:   # fell far behind (system suspended?) - resync
                next_t = time.monotonic()
        self.backend.safe_state()

    def start(self) -> None:
        self.backend.safe_state()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="io", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2)
        self.backend.close()
