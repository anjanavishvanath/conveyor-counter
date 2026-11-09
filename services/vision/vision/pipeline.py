"""
services/vision/vision/pipeline.py

One frame in -> detections -> tracks -> count events out.
The same class is used by the live vision service and by the offline evaluation tool,
so what you measure offline is exactly what runs in production.
"""
from __future__ import annotations

import time
from typing import Optional

import numpy as np

from vision.config import ProductProfile
from vision.counting.line_counter import LineCounter
from vision.detectors.base import Detector
from vision.tracking.tracker import Tracker
from vision.types import Detections, Frame, FrameResult


class CountingPipeline:
    def __init__(self, profile: ProductProfile, detector: Optional[Detector]):
        self.profile = profile
        self.detector = detector  # may be None when replaying cached detections
        self.tracker = Tracker(profile.tracking)
        self.counter = LineCounter(profile.counting)
        self._count_classes = np.array(profile.model.count_class_ids, dtype=np.int32)
        self.last_raw_detections = Detections.empty()
        self._last_frame_id = None

    def reset(self) -> None:
        """Clear tracks and the running total (e.g. new evaluation run)."""
        self.tracker.reset()
        self.counter.reset()
        self._last_frame_id = None

    def _filter(self, dets: Detections, w: int, h: int) -> Detections:
        if len(dets) == 0:
            return dets
        x1, y1, x2, y2 = self.profile.counting.roi
        cx = (dets.boxes[:, 0] + dets.boxes[:, 2]) / 2 / w
        cy = (dets.boxes[:, 1] + dets.boxes[:, 3]) / 2 / h
        keep = (cx >= x1) & (cx <= x2) & (cy >= y1) & (cy <= y2) & np.isin(dets.class_ids, self._count_classes)
        return dets.select(keep)

    def process(self, frame: Frame, detections: Optional[Detections] = None) -> FrameResult:
        """Run one frame. Pass `detections` to skip the model (replay of cached detections)."""
        h, w = frame.image.shape[:2]
        timings = {}
        if detections is None:
            if self.detector is None:
                raise RuntimeError("No detector and no detections given")
            detect_frame = getattr(self.detector, "detect_frame", None)
            detections = detect_frame(frame) if detect_frame else self.detector.detect(frame.image)
            timings.update(self.detector.last_timings_ms)
        self.last_raw_detections = detections  # before ROI/class filtering (kept for caching and debugging)
        t0 = time.perf_counter()
        dets = self._filter(detections, w, h)
        steps = 1 if self._last_frame_id is None else max(1, frame.frame_id - self._last_frame_id)
        self._last_frame_id = frame.frame_id
        tracks = self.tracker.update(dets, steps=steps)
        events = self.counter.update(tracks, self.tracker.removed_ids, w, h, frame.frame_id, frame.timestamp)
        timings["track_count"] = (time.perf_counter() - t0) * 1e3
        return FrameResult(frame, dets, tracks, events, self.counter.stats.total, timings)
