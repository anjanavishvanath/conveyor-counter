"""
services/vision/vision/main.py — entry point for the vision service.

    python -m vision.main                      # uses config/site.yaml (or $COUNTER_SITE_CONFIG)
    python -m vision.main --config path/site.yaml
    python -m vision.main --stride 2           # every 2nd frame: simulate a slower board
    python -m vision.main --exit-on-end        # stop when a video file finishes (CI / demos)

Logging goes to stderr (journald captures it under systemd) and optionally to a rotating file.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from vision.config import ConfigError, load_config
from vision.service import run_service


def setup_logging(level: str, log_file: str | None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-8s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(fmt)
    root.addHandler(stderr)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        rotating.setFormatter(fmt)
        root.addHandler(rotating)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Conveyor counting vision service")
    ap.add_argument("--config", default=None, help="path to site.yaml")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--log-file", default=None, help="also write a rotating log file here")
    ap.add_argument("--stride", type=int, default=1, help="process every Nth frame (video source only)")
    ap.add_argument("--exit-on-end", action="store_true", help="exit when the video source ends")
    args = ap.parse_args(argv)

    setup_logging(args.log_level, args.log_file)
    log = logging.getLogger("main")

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("CONFIG ERROR: %s", e)
        return 2

    if not cfg.profile.model.file.is_file():
        log.error("model file not found: %s", cfg.profile.model.file)
        return 2

    log.info("site '%s', product '%s', config %s", cfg.site.site_name, cfg.profile.product, cfg.site.path)
    return run_service(cfg, exit_on_end=args.exit_on_end, stride=args.stride)


if __name__ == "__main__":
    sys.exit(main())
