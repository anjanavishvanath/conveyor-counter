"""
services/api/api/main.py — entry point for the API / kiosk backend.

    cd services/api
    python -m api.main                       # config/site.yaml (or $COUNTER_SITE_CONFIG)
    python -m api.main --config path/site.yaml --port 8000
"""
from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from api.settings import ConfigError, load_config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Conveyor counter API")
    ap.add_argument("--config", default=None, help="path to site.yaml")
    ap.add_argument("--host", default=None, help="override api.host")
    ap.add_argument("--port", type=int, default=None, help="override api.port")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args(argv)

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    log = logging.getLogger("api")
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("CONFIG ERROR: %s", e)
        return 2

    from api.app import create_app
    app = create_app(cfg)
    host = args.host or cfg.site.api.host
    port = args.port or cfg.site.api.port
    log.info("listening on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower(), access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
