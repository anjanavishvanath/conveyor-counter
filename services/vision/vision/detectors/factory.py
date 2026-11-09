"""services/vision/vision/detectors/factory.py — build the detector selected in site.yaml."""
from __future__ import annotations

from vision.config import AppConfig
from vision.detectors.base import Detector


def create_detector(cfg: AppConfig) -> Detector:
    backend = cfg.site.detector.backend
    if backend == "onnx_cpu":
        from vision.detectors.onnx_cpu import OnnxCpuDetector
        return OnnxCpuDetector(cfg.profile, threads=cfg.site.detector.onnx_cpu.threads)
    if backend == "replay":  # DEV ONLY: config validation guarantees video file + simulated IO
        from vision.detectors.replay import ReplayDetector
        return ReplayDetector(cfg.site.detector.replay.dets_file)
    if backend == "qnn_htp":
        raise NotImplementedError("NPU (QNN HTP) detector is added in checkpoint 8, on the board")
    raise ValueError(f"Unknown detector backend: {backend}")
