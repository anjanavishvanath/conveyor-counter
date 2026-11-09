"""services/vision/vision/detectors/base.py — detector interface (ONNX CPU on PC, QNN NPU on board)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict

import numpy as np

from vision.types import Detections


class Detector(ABC):
    @abstractmethod
    def detect(self, image_bgr: np.ndarray) -> Detections:
        """Run the model on one BGR frame; return boxes in that frame's pixel coordinates."""

    def detect_frame(self, frame) -> Detections:
        """Run on a Frame. Default: detect(frame.image). The dev replay detector needs the frame position."""
        return self.detect(frame.image)

    @property
    def last_timings_ms(self) -> Dict[str, float]:
        """Per-stage timings of the last detect() call: preprocess / inference / postprocess."""
        return {}

    @property
    def device(self) -> str:
        """Where inference runs: 'cpu', 'npu' or 'gpu' — reported in the UI and logs."""
        return "cpu"
