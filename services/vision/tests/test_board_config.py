"""
services/vision/tests/test_board_config.py

config/site.board.yaml must always load, and bench-only values must never pass with real IO.
"""
from pathlib import Path

import pytest
import yaml

from vision.config import REPO_ROOT, ConfigError, load_config

BOARD = REPO_ROOT / "config" / "site.board.yaml"


def test_board_config_loads():
    cfg = load_config(BOARD)
    hk = cfg.site.frame_source.hik_mvs
    assert cfg.site.frame_source.type == "hik_mvs" and hk.ip
    assert hk.pixel_format.startswith("Bayer")        # RGB8 at 30 fps does not fit through GigE
    assert cfg.site.detector.backend != "replay"


def test_bench_min_fps_refused_with_real_io(tmp_path: Path):
    data = yaml.safe_load(BOARD.read_text(encoding="utf-8"))
    data["io"]["backend"] = "gpiod"
    data["safety"]["min_fps"] = 3.0
    data["product_profile"] = str((BOARD.parent / data["product_profile"]).resolve())
    p = tmp_path / "site.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="bench value"):
        load_config(p)
    data["safety"]["min_fps"] = 20.0
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    load_config(p)
