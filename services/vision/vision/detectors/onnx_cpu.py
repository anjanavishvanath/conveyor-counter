"""
services/vision/vision/detectors/onnx_cpu.py

YOLO detector on ONNX Runtime, CPU execution provider.
Runs on: CPU (PC for development; also works on the board's CPU as a slow fallback).
The board's NPU backend (checkpoint 8) reuses yolo_common, so boxes are post-processed identically.
"""
from __future__ import annotations

import os
import time
from typing import Dict

import numpy as np
import onnxruntime as ort

from vision.config import ProductProfile
from vision.detectors.base import Detector
from vision.detectors.yolo_common import decode, letterbox, to_nchw_float
from vision.types import Detections


class OnnxCpuDetector(Detector):
    def __init__(self, profile: ProductProfile, threads: int = 4):
        self.p = profile
        path = profile.model.file
        if not path.is_file():
            raise FileNotFoundError(f"Model file not found: {path}")
        so = ort.SessionOptions()
        # More threads than cores makes ONNX Runtime much slower, not faster
        so.intra_op_num_threads = max(1, min(threads, os.cpu_count() or 1))
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        shape = inp.shape  # e.g. [1, 3, 640, 640]
        if isinstance(shape[2], int) and isinstance(shape[3], int):
            if (shape[3], shape[2]) != (profile.model.input_width, profile.model.input_height):
                raise ValueError(f"Model input is {shape[3]}x{shape[2]} but profile says "
                                 f"{profile.model.input_width}x{profile.model.input_height}")
        nc_model = self.session.get_outputs()[0].shape[1] - 4
        if isinstance(nc_model, int) and nc_model != len(profile.model.class_names):
            raise ValueError(f"Model has {nc_model} classes but profile lists {len(profile.model.class_names)}")
        self._t: Dict[str, float] = {}

    @property
    def device(self) -> str:
        return "cpu"

    @property
    def last_timings_ms(self) -> Dict[str, float]:
        return self._t

    def detect(self, image_bgr: np.ndarray) -> Detections:
        m, d = self.p.model, self.p.detection
        t0 = time.perf_counter()
        img, lb = letterbox(image_bgr, m.input_width, m.input_height)
        blob = to_nchw_float(img)
        t1 = time.perf_counter()
        out = self.session.run(None, {self.input_name: blob})[0]
        t2 = time.perf_counter()
        h, w = image_bgr.shape[:2]
        dets = decode(out, lb, w, h, d.conf_threshold, d.nms_iou, d.max_detections)
        t3 = time.perf_counter()
        self._t = {"pre": (t1 - t0) * 1e3, "infer": (t2 - t1) * 1e3, "post": (t3 - t2) * 1e3}
        return dets
