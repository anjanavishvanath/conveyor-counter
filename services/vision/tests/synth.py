"""services/vision/tests/synth.py — synthetic conveyor scenes for tracker/counter tests (no model needed)."""
from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np

from vision.types import Detections

W, H = 1280, 720


def moving_objects(n_objects: int, spacing_frames: int, speed_px: float, size: float = 120,
                   x_positions: Optional[Sequence[float]] = None, n_frames: Optional[int] = None,
                   score: float = 0.9, start_y: float = -60) -> List[Detections]:
    """
    Objects enter at the top, move down at speed_px/frame, one every spacing_frames.
    Returns a Detections per frame (boxes fully off-screen are dropped, partial boxes are clipped).
    """
    xs = x_positions or [W / 2]
    total = n_frames or (n_objects * spacing_frames + int((H - start_y + size) / speed_px) + 5)
    frames = []
    for f in range(total):
        boxes = []
        for i in range(n_objects):
            t = f - i * spacing_frames
            if t < 0:
                continue
            cy = start_y + t * speed_px
            cx = xs[i % len(xs)]
            b = [cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2]
            if b[3] <= 0 or b[1] >= H:
                continue
            b = [max(0, b[0]), max(0, b[1]), min(W, b[2]), min(H, b[3])]
            if b[3] - b[1] < size * 0.3:  # detector wouldn't see a sliver
                continue
            boxes.append(b)
        frames.append(Detections(np.array(boxes, np.float32).reshape(-1, 4),
                                 np.full(len(boxes), score, np.float32),
                                 np.zeros(len(boxes), np.int32)))
    return frames
