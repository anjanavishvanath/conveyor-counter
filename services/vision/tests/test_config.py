"""services/vision/tests/test_config.py — checkpoint 1"""
from pathlib import Path

import pytest
import yaml

from vision.config import DEFAULT_SITE_CONFIG, ConfigError, load_config


def test_repo_config_loads():
    cfg = load_config(DEFAULT_SITE_CONFIG)
    assert cfg.site.crates.count == 12
    assert cfg.profile.product == "coconut"
    assert cfg.profile.model.file.is_absolute()
    assert cfg.site.storage.db_path.is_absolute()


def _write_site(tmp_path: Path, mutate) -> Path:
    site = yaml.safe_load(DEFAULT_SITE_CONFIG.read_text())
    site["product_profile"] = str(DEFAULT_SITE_CONFIG.parent / site["product_profile"])
    mutate(site)
    p = tmp_path / "site.yaml"
    p.write_text(yaml.safe_dump(site))
    return p


def test_unknown_key_rejected(tmp_path):
    p = _write_site(tmp_path, lambda s: s["safety"].update({"frame_timout_ms": 5}))  # typo
    with pytest.raises(ConfigError, match="frame_timout_ms"):
        load_config(p)


def test_duplicate_gpio_rejected(tmp_path):
    def m(s):
        s["io"]["backend"] = "gpiod"
        s["io"]["gpiod"]["alarm_light"]["line"] = s["io"]["gpiod"]["run_permit"]["line"]
    with pytest.raises(ConfigError, match="unique"):
        load_config(_write_site(tmp_path, m))


def test_missing_source_section_rejected(tmp_path):
    def m(s):
        s["frame_source"]["type"] = "hik_mvs"
        s["frame_source"].pop("hik_mvs")
    with pytest.raises(ConfigError, match="hik_mvs"):
        load_config(_write_site(tmp_path, m))
