"""services/vision/vision/types.py — data passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import numpy as np


@dataclass
class Frame:
    image: np.ndarray        # BGR, HxWx3
    frame_id: int            # camera/file frame number; gaps mean lost frames
    timestamp: float         # seconds (monotonic for cameras, video time for files)


@dataclass
class Detections:
    boxes: np.ndarray        # (N, 4) float32 x1, y1, x2, y2 in frame pixels
    scores: np.ndarray       # (N,) float32
    class_ids: np.ndarray    # (N,) int32

    @staticmethod
    def empty() -> "Detections":
        return Detections(np.zeros((0, 4), np.float32), np.zeros((0,), np.float32), np.zeros((0,), np.int32))

    def __len__(self) -> int:
        return len(self.scores)

    def select(self, mask: np.ndarray) -> "Detections":
        return Detections(self.boxes[mask], self.scores[mask], self.class_ids[mask])


@dataclass
class TrackOut:
    track_id: int
    box: np.ndarray          # (4,) x1, y1, x2, y2
    score: float
    hits: int                # number of frames with a matched detection
    age: int                 # frames since the track was created
    updated: bool            # True if matched to a detection in this frame (False = coasting on prediction)


@dataclass
class CountEvent:
    track_id: int
    frame_id: int
    timestamp: float


@dataclass
class FrameResult:
    frame: Frame
    detections: Detections
    tracks: List[TrackOut]
    new_counts: List[CountEvent]
    total: int
    timings_ms: Dict[str, float] = field(default_factory=dict)
