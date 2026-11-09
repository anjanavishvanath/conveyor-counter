#!/usr/bin/env python3
"""
tools/board/gpio_check.py — wiring check for the four field signals (run ON THE BOARD, checkpoint 10)

Uses the same GpiodIo class and site.yaml pin map as the vision service.
- Prints the start/stop button states live (press them and watch).
- 'p' toggles the run-permit relay, 'a' toggles the alarm light, 'q' quits.
The relay is forced OFF on exit, on Ctrl-C and on any error.

!!! Do this with the conveyor motor isolated (MCB off / motor disconnected) the first time.

Assumes: Radxa Dragon Q6A, Linux with /dev/gpiochipN and python gpiod>=2.0.
UNVERIFIED: gpiochip/line numbers in site.yaml must first be looked up with `gpioinfo`.
"""
import select
import sys
import termios
import time
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "vision"))

from vision.config import load_config  # noqa: E402
from vision.io.gpiod_io import GpiodIo  # noqa: E402


def main():
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    if cfg.site.io.gpiod is None:
        print("io.gpiod is not configured in site.yaml")
        return 2
    io = GpiodIo(cfg.site.io.gpiod)
    permit, alarm = False, True
    old = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        print("p = toggle relay, a = toggle alarm light, q = quit\n")
        while True:
            io.write_outputs(permit, alarm)
            i = io.read_inputs()
            print(f"\rSTART {'PRESSED' if i.start else 'released'}   "
                  f"STOP {'ACTIVE (pressed/open circuit)' if i.stop else 'healthy'}   "
                  f"relay {'ON ' if permit else 'OFF'}   alarm {'ON ' if alarm else 'OFF'}   ", end="", flush=True)
            if select.select([sys.stdin], [], [], 0.05)[0]:
                k = sys.stdin.read(1).lower()
                if k == "q":
                    break
                if k == "p":
                    permit = not permit
                if k == "a":
                    alarm = not alarm
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        io.close()
        print("\nrelay OFF, lines released")


if __name__ == "__main__":
    sys.exit(main())
