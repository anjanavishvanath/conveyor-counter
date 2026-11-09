"""services/vision/vision/io/factory.py — build the IO backend selected in site.yaml."""
from __future__ import annotations

from vision.config import IoCfg
from vision.io.base import IoBackend


def create_io(cfg: IoCfg) -> IoBackend:
    if cfg.backend == "sim":
        from vision.io.sim import SimIo
        return SimIo()
    if cfg.backend == "gpiod":
        from vision.io.gpiod_io import GpiodIo
        return GpiodIo(cfg.gpiod)
    raise ValueError(f"Unknown io backend: {cfg.backend}")
