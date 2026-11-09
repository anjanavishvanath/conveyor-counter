"""
services/api/api/settings.py

The API reads the same config/site.yaml as the vision service, through the vision package's
validated loader, so both services always agree on crates, targets, topics and paths.
It also reads the vision database through `vision.store.db` (read-only use), so the schema
and the CSV report format have exactly one definition.

Only these two pure modules are shared (no OpenCV, no ONNX Runtime): the API's environment
needs PyYAML + pydantic, nothing heavy.
"""
from __future__ import annotations

import sys
from pathlib import Path

# <repo>/services/api/api/settings.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[3]
_VISION_PKG = REPO_ROOT / "services" / "vision"
if str(_VISION_PKG) not in sys.path:
    sys.path.insert(0, str(_VISION_PKG))

from vision.config import AppConfig, ConfigError, load_config  # noqa: E402

__all__ = ["AppConfig", "ConfigError", "load_config", "REPO_ROOT"]
