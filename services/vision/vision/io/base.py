"""
services/vision/vision/io/base.py — the four field signals.

  run_permit   (output) relay in the DOL control circuit. Energized = conveyor ALLOWED to run.
               De-energized = contactor drops. Anything that kills the software (crash, power loss,
               board reset) de-energizes it, so the failure direction is always "stop".
  alarm_light  (output) ON while the conveyor is not permitted to run; flashing = fault.
  start_button (input)  NO push button. Only a press (released -> pressed edge) starts; holding it does nothing.
  stop_button  (input)  NC push button wired so that "active" = pressed OR wire broken OR supply lost.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Inputs:
    start: bool   # True = start button pressed
    stop: bool    # True = stop button pressed (or its circuit is open)


class IoBackend(ABC):
    @abstractmethod
    def read_inputs(self) -> Inputs: ...

    @abstractmethod
    def write_outputs(self, run_permit: bool, alarm_light: bool) -> None: ...

    def safe_state(self) -> None:
        """Permit OFF, alarm ON. Called on shutdown and on any IO error."""
        try:
            self.write_outputs(run_permit=False, alarm_light=True)
        except Exception:
            pass

    def close(self) -> None:
        self.safe_state()
