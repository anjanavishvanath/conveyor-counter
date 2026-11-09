"""
services/vision/vision/io/gpiod_io.py — field IO on the board's GPIO header via libgpiod v2.

Assumes: Radxa Dragon Q6A running a Linux image with the kernel GPIO character device
(/dev/gpiochipN) and the libgpiod v2 Python bindings (`pip install gpiod>=2.0`).
!!! UNVERIFIED on the Q6A: which gpiochip and line offsets map to which 40-pin header pins.
    Run `gpioinfo` / `gpiodetect` on the board and set config/site.yaml io.gpiod accordingly.

Electrical notes (do not skip):
- The GPIO pins are 3.3 V logic. Drive the relay through a transistor/driver relay module, and bring
  24 V push buttons in through an opto-isolated input module. Never wire 24 V to a GPIO pin.
- Polarity is set per pin with active_low in site.yaml, so the code always works in terms of
  "pressed" / "energized" regardless of how the interface module inverts signals.
"""
from __future__ import annotations

from vision.config import GpiodCfg
from vision.io.base import Inputs, IoBackend

try:
    import gpiod
    from gpiod.line import Direction, Value
except ImportError:  # PC without libgpiod: importing this module must not break the rest of the code
    gpiod = None


class GpiodIo(IoBackend):
    def __init__(self, cfg: GpiodCfg):
        if gpiod is None:
            raise RuntimeError("python 'gpiod' (libgpiod v2 bindings) is not installed")
        self.cfg = cfg

        def out(pin):
            # Outputs are requested already INACTIVE: the relay is never energized during start-up
            return gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE,
                                      active_low=pin.active_low)

        def inp(pin):
            return gpiod.LineSettings(direction=Direction.INPUT, active_low=pin.active_low)

        self._req = gpiod.request_lines(
            cfg.chip,
            consumer="conveyor-counter",
            config={
                cfg.run_permit.line: out(cfg.run_permit),
                cfg.alarm_light.line: out(cfg.alarm_light),
                cfg.start_button.line: inp(cfg.start_button),
                cfg.stop_button.line: inp(cfg.stop_button),
            },
        )
        self.write_outputs(run_permit=False, alarm_light=True)

    def read_inputs(self) -> Inputs:
        start = self._req.get_value(self.cfg.start_button.line) == Value.ACTIVE
        stop = self._req.get_value(self.cfg.stop_button.line) == Value.ACTIVE
        return Inputs(start=start, stop=stop)

    def write_outputs(self, run_permit: bool, alarm_light: bool) -> None:
        self._req.set_values({
            self.cfg.run_permit.line: Value.ACTIVE if run_permit else Value.INACTIVE,
            self.cfg.alarm_light.line: Value.ACTIVE if alarm_light else Value.INACTIVE,
        })

    def close(self) -> None:
        self.safe_state()
        try:
            self._req.release()
        except Exception:
            pass
