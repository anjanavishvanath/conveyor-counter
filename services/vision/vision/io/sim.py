"""
services/vision/vision/io/sim.py — simulated field IO for PC development and tests.

Buttons are "pressed" from code (and later from the dev UI through the API);
output changes are kept in a log so tests can check exactly what the relay did.
"""
from __future__ import annotations

import threading
import time
from typing import List, Tuple

from vision.io.base import Inputs, IoBackend


class SimIo(IoBackend):
    def __init__(self):
        self._lock = threading.Lock()
        self._start = False
        self._stop = False
        self.run_permit = False
        self.alarm_light = True
        self.log: List[Tuple[float, str, bool]] = []  # (time, output name, new value)
        self.fail_reads = False                       # tests: simulate a broken IO driver

    # ── test / dev-UI controls ──
    def set_start(self, pressed: bool) -> None:
        with self._lock:
            self._start = pressed

    def set_stop(self, pressed: bool) -> None:
        with self._lock:
            self._stop = pressed

    def press_start(self, hold_s: float = 0.1) -> None:
        self.set_start(True)
        time.sleep(hold_s)
        self.set_start(False)

    def press_stop(self, hold_s: float = 0.1) -> None:
        self.set_stop(True)
        time.sleep(hold_s)
        self.set_stop(False)

    # ── IoBackend ──
    def read_inputs(self) -> Inputs:
        if self.fail_reads:
            raise OSError("simulated IO failure")
        with self._lock:
            return Inputs(start=self._start, stop=self._stop)

    def write_outputs(self, run_permit: bool, alarm_light: bool) -> None:
        with self._lock:
            now = time.monotonic()
            if run_permit != self.run_permit:
                self.log.append((now, "run_permit", run_permit))
            if alarm_light != self.alarm_light:
                self.log.append((now, "alarm_light", alarm_light))
            self.run_permit, self.alarm_light = run_permit, alarm_light
