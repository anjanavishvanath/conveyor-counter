"""
services/vision/vision/config.py

Loads and validates config/site.yaml and the product profile it points to.
Relative paths are resolved against the directory of the YAML file that contains them,
so the same config works on the PC, in Docker and on the board.

Usage:
    from vision.config import load_config
    cfg = load_config()                       # uses $COUNTER_SITE_CONFIG or ../../config/site.yaml
    cfg = load_config("path/to/site.yaml")
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Default: <repo>/config/site.yaml (this file is <repo>/services/vision/vision/config.py)
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SITE_CONFIG = REPO_ROOT / "config" / "site.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos in YAML keys become errors, not silent defaults


# ─── Product profile ────────────────────────────────────────────────────────
class ModelCfg(_Strict):
    file: Path
    input_width: int = Field(640, gt=0)
    input_height: int = Field(640, gt=0)
    class_names: List[str]
    count_class_ids: List[int] = [0]

    @model_validator(mode="after")
    def _check_ids(self):
        for cid in self.count_class_ids:
            if not 0 <= cid < len(self.class_names):
                raise ValueError(f"count_class_ids contains {cid}, but only {len(self.class_names)} classes exist")
        if self.input_width % 32 or self.input_height % 32:
            raise ValueError("model input size must be a multiple of 32")
        return self


class DetectionCfg(_Strict):
    conf_threshold: float = Field(0.25, gt=0, lt=1)
    nms_iou: float = Field(0.5, gt=0, lt=1)
    max_detections: int = Field(100, gt=0)


class TrackingCfg(_Strict):
    high_conf: float = Field(0.5, gt=0, lt=1)
    match_iou: float = Field(0.2, gt=0, lt=1)
    max_age_frames: int = Field(10, ge=1)
    min_hits: int = Field(3, ge=1)
    center_gate: float = Field(0.6, ge=0, le=2)   # 0 disables centre-distance matching
    belt_velocity_prior: bool = True


class CountingCfg(_Strict):
    axis: Literal["x", "y"] = "y"
    line_position: float = Field(0.5, gt=0, lt=1)
    direction: Literal["positive", "negative"] = "positive"
    hysteresis: float = Field(0.03, ge=0, lt=0.25)
    min_track_hits: int = Field(3, ge=1)
    roi: List[float] = [0.0, 0.0, 1.0, 1.0]

    @field_validator("roi")
    @classmethod
    def _roi(cls, v):
        if len(v) != 4 or not (0 <= v[0] < v[2] <= 1 and 0 <= v[1] < v[3] <= 1):
            raise ValueError("roi must be [x1, y1, x2, y2] with 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1")
        return v


class ProductProfile(_Strict):
    product: str
    model: ModelCfg
    detection: DetectionCfg = DetectionCfg()
    tracking: TrackingCfg = TrackingCfg()
    counting: CountingCfg = CountingCfg()
    path: Optional[Path] = None  # set by the loader

    @model_validator(mode="after")
    def _check_conf(self):
        if self.tracking.high_conf < self.detection.conf_threshold:
            raise ValueError("tracking.high_conf must be >= detection.conf_threshold")
        return self


# ─── Site ───────────────────────────────────────────────────────────────────
class CratesCfg(_Strict):
    count: int = Field(12, ge=1, le=64)
    default_target: int = Field(400, ge=1)
    max_target: int = Field(2000, ge=1)


class VideoFileCfg(_Strict):
    path: Path
    realtime: bool = True
    loop: bool = False


class HikMvsCfg(_Strict):
    """Hikrobot GigE camera through the MVS SDK (vision/sources/hik_mvs.py)."""
    ip: Optional[str] = None                 # open the camera with this IP ...
    serial: Optional[str] = None             # ... or this serial number; neither = the only camera found
    pixel_format: Literal["BayerRG8", "BayerGB8", "BayerGR8", "BayerBG8", "Mono8", "BGR8Packed", "RGB8Packed"] = "BayerRG8"
    exposure_us: float = Field(2000, gt=0)   # fixed exposure: auto exposure is switched off
    gain_db: float = Field(0.0, ge=0)        # fixed gain: auto gain is switched off
    fps: float = Field(30, gt=0, le=200)     # camera frame rate (AcquisitionFrameRate)
    packet_size: int = Field(0, ge=0)        # 0 = the SDK's optimal packet size (needs jumbo frames for 8000+)
    buffer_frames: int = Field(3, ge=1, le=64)
    sdk_python_path: Path = Path("/opt/MVS/Samples/aarch64/Python/MvImport")
    sdk_lib: Path = Path("/opt/MVS/lib/aarch64/libMvCameraControl.so")


class FrameSourceCfg(_Strict):
    type: Literal["video_file", "hik_mvs"] = "video_file"
    video_file: Optional[VideoFileCfg] = None
    hik_mvs: Optional[HikMvsCfg] = None

    @model_validator(mode="after")
    def _selected_present(self):
        if getattr(self, self.type) is None:
            raise ValueError(f"frame_source.type is '{self.type}' but frame_source.{self.type} is missing")
        return self


class OnnxCpuCfg(_Strict):
    threads: int = Field(4, ge=1)


class ReplayCfg(_Strict):
    """DEV ONLY: replay detections cached by tools/eval/count_eval.py --save-dets (stride 1)."""
    dets_file: Path


class DetectorCfg(_Strict):
    backend: Literal["onnx_cpu", "qnn_htp", "replay"] = "onnx_cpu"
    onnx_cpu: OnnxCpuCfg = OnnxCpuCfg()
    replay: Optional[ReplayCfg] = None


class PinCfg(_Strict):
    line: int = Field(ge=0)
    active_low: bool = False


class GpiodCfg(_Strict):
    chip: str
    run_permit: PinCfg
    alarm_light: PinCfg
    start_button: PinCfg
    stop_button: PinCfg

    @model_validator(mode="after")
    def _unique_lines(self):
        lines = [self.run_permit.line, self.alarm_light.line, self.start_button.line, self.stop_button.line]
        if len(set(lines)) != len(lines):
            raise ValueError(f"GPIO lines must be unique, got {lines}")
        return self


class IoCfg(_Strict):
    backend: Literal["sim", "gpiod"] = "sim"
    gpiod: Optional[GpiodCfg] = None
    debounce_ms: int = Field(30, ge=0, le=500)
    alarm_flash_on_fault: bool = True

    @model_validator(mode="after")
    def _gpiod_present(self):
        if self.backend == "gpiod" and self.gpiod is None:
            raise ValueError("io.backend is 'gpiod' but io.gpiod is missing")
        return self


class SafetyCfg(_Strict):
    frame_timeout_ms: int = Field(1000, ge=100)
    max_frame_gap: int = Field(2, ge=1)
    min_fps: float = Field(10.0, gt=0)
    min_fps_window_s: float = Field(3.0, gt=0)
    clear_time_s: float = Field(5.0, gt=0)
    heartbeat_period_ms: int = Field(250, ge=50)
    # A database write failure means counts are not being persisted -> silent miscount risk.
    fault_on_db_error: bool = True
    # A broker loss only blinds the UI; counting and the interlock are unaffected.
    # True = stop the conveyor when the supervisor can no longer see the count (strict);
    # False = keep running and show the broker as down. Decide with the plant.
    fault_on_bus_loss: bool = False
    bus_grace_s: float = Field(10.0, gt=0)      # allow this long for a reconnect first


class StorageCfg(_Strict):
    db_path: Path
    # FULL = fsync on every commit: a power loss costs at most the writer queue.
    # NORMAL is faster but can lose the last commits on an unclean shutdown.
    synchronous: Literal["FULL", "NORMAL"] = "FULL"


class MqttCfg(_Strict):
    host: str = "localhost"
    port: int = 1883
    base_topic: str = "counter"
    keepalive_s: int = Field(15, ge=5)
    username: Optional[str] = None
    password: Optional[str] = None


class ReportingCfg(_Strict):
    """A production day runs from day_start (site local time) to day_start the next day."""
    day_start: str = "00:00"

    @field_validator("day_start")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        try:
            hh, mm = v.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except Exception:
            raise ValueError("reporting.day_start must be 'HH:MM' in 24-hour local time") from None
        return v


class PreviewCfg(_Strict):
    """Annotated MJPEG stream served by the vision service itself (commissioning + count disputes)."""
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(8081, ge=1, le=65535)
    fps: float = Field(5.0, gt=0, le=30)
    jpeg_quality: int = Field(70, ge=20, le=95)
    max_width: int = Field(960, ge=160)


class ApiCfg(_Strict):
    """The API / kiosk backend (services/api). The vision service ignores this section."""
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    settings_db: Path = Path("../data/api.db")       # crate presets; the API is its only writer
    command_timeout_s: float = Field(3.0, gt=0, le=30)
    export_dir: Path = Path("../data/exports")       # CSV export target when no USB stick is mounted
    usb_mount_roots: List[str] = ["/media", "/run/media"]
    ui_dist: Path = Path("../services/ui/dist")      # built kiosk UI, served at / when present
    # Shutdown / reboot / restart buttons. Off on a PC. On the board, enable it and allow exactly
    # these commands for the service user in /etc/sudoers.d (sudo -n never prompts).
    allow_system_control: bool = False
    shutdown_cmd: List[str] = ["sudo", "-n", "/usr/bin/systemctl", "poweroff"]
    reboot_cmd: List[str] = ["sudo", "-n", "/usr/bin/systemctl", "reboot"]
    restart_cmd: List[str] = ["sudo", "-n", "/usr/bin/systemctl", "restart", "counter-vision.service"]


class SiteConfig(_Strict):
    site_name: str
    timezone: str = "UTC"
    product_profile: Path
    crates: CratesCfg = CratesCfg()
    frame_source: FrameSourceCfg
    detector: DetectorCfg = DetectorCfg()
    io: IoCfg = IoCfg()
    safety: SafetyCfg = SafetyCfg()
    storage: StorageCfg
    mqtt: MqttCfg = MqttCfg()
    reporting: ReportingCfg = ReportingCfg()
    preview: PreviewCfg = PreviewCfg()
    api: ApiCfg = ApiCfg()
    path: Optional[Path] = None

    @model_validator(mode="after")
    def _no_bench_values_on_real_io(self):
        # A bench test with the CPU detector needs a low min_fps. That value must never reach a
        # wired conveyor: below ~10 fps the counter misses coconuts (see decisions log).
        if self.io.backend == "gpiod" and self.safety.min_fps < 10:
            raise ValueError(f"safety.min_fps={self.safety.min_fps} is a bench value; with io.backend 'gpiod' "
                             "it must be >= 10 (derive it from belt speed and the camera view)")
        return self

    @model_validator(mode="after")
    def _replay_is_dev_only(self):
        if self.detector.backend == "replay":
            if self.detector.replay is None:
                raise ValueError("detector.backend is 'replay' but detector.replay.dets_file is missing")
            if self.frame_source.type != "video_file" or self.io.backend != "sim":
                raise ValueError("detector.backend 'replay' is for PC development only: it needs "
                                 "frame_source.type 'video_file' and io.backend 'sim'")
        return self

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            raise ValueError(f"unknown timezone '{v}' (use an IANA name such as 'Asia/Colombo')") from None
        return v


class AppConfig(BaseModel):
    site: SiteConfig
    profile: ProductProfile


# ─── Loading ────────────────────────────────────────────────────────────────
class ConfigError(RuntimeError):
    pass


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _resolve(base: Path, p: Path) -> Path:
    return p if p.is_absolute() else (base / p).resolve()


def load_profile(path: str | Path) -> ProductProfile:
    path = Path(path).resolve()
    try:
        prof = ProductProfile(**_read_yaml(path))
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Invalid product profile {path}:\n{e}") from e
    prof.model.file = _resolve(path.parent, prof.model.file)
    prof.path = path
    return prof


def load_config(site_path: str | Path | None = None) -> AppConfig:
    site_path = Path(site_path or os.environ.get("COUNTER_SITE_CONFIG", DEFAULT_SITE_CONFIG)).resolve()
    try:
        site = SiteConfig(**_read_yaml(site_path))
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Invalid site config {site_path}:\n{e}") from e

    base = site_path.parent
    site.path = site_path
    site.product_profile = _resolve(base, site.product_profile)
    site.storage.db_path = _resolve(base, site.storage.db_path)
    if site.frame_source.video_file:
        site.frame_source.video_file.path = _resolve(base, site.frame_source.video_file.path)
    if site.detector.replay:
        site.detector.replay.dets_file = _resolve(base, site.detector.replay.dets_file)
    site.api.settings_db = _resolve(base, site.api.settings_db)
    site.api.export_dir = _resolve(base, site.api.export_dir)
    site.api.ui_dist = _resolve(base, site.api.ui_dist)

    return AppConfig(site=site, profile=load_profile(site.product_profile))


if __name__ == "__main__":
    # Checkpoint 1: `python -m vision.config` prints the resolved config or a readable error.
    import json
    import sys

    try:
        cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(cfg.model_dump(mode="json"), indent=2))
    print(f"\nOK - site '{cfg.site.site_name}', product '{cfg.profile.product}', "
          f"model {'found' if cfg.profile.model.file.is_file() else 'MISSING'}: {cfg.profile.model.file}")
