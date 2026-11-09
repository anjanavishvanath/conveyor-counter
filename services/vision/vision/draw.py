"""services/vision/vision/draw.py — annotated preview frames (evaluation video now, UI preview later)."""
from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from vision.config import CountingCfg
from vision.types import FrameResult

GREEN, ORANGE, RED, WHITE, GREY = (80, 200, 80), (0, 165, 255), (60, 60, 230), (255, 255, 255), (160, 160, 160)


def annotate(result: FrameResult, counting: CountingCfg, counted_ids: Optional[set] = None,
             extra_text: str = "") -> np.ndarray:
    img = result.frame.image.copy()
    h, w = img.shape[:2]
    counted_ids = counted_ids or set()
    th = max(1, int(round(min(w, h) / 360)))
    fs = min(w, h) / 900

    # Counting line and dead band
    lp, hy = counting.line_position, counting.hysteresis
    if counting.axis == "y":
        y, y0, y1 = int(lp * h), int((lp - hy) * h), int((lp + hy) * h)
        overlay = img.copy()
        cv2.rectangle(overlay, (0, y0), (w, y1), RED, -1)
        img = cv2.addWeighted(overlay, 0.15, img, 0.85, 0)
        cv2.line(img, (0, y), (w, y), RED, th + 1)
    else:
        x, x0, x1 = int(lp * w), int((lp - hy) * w), int((lp + hy) * w)
        overlay = img.copy()
        cv2.rectangle(overlay, (x0, 0), (x1, h), RED, -1)
        img = cv2.addWeighted(overlay, 0.15, img, 0.85, 0)
        cv2.line(img, (x, 0), (x, h), RED, th + 1)

    # Raw detections (thin grey) and tracks (green = counted, orange = not yet counted)
    for b in result.detections.boxes.astype(int):
        cv2.rectangle(img, (b[0], b[1]), (b[2], b[3]), GREY, 1)
    for t in result.tracks:
        b = t.box.astype(int)
        col = GREEN if t.track_id in counted_ids else ORANGE
        cv2.rectangle(img, (b[0], b[1]), (b[2], b[3]), col, th if t.updated else 1)
        cv2.putText(img, f"{t.track_id}", (b[0], max(12, b[1] - 4)), cv2.FONT_HERSHEY_SIMPLEX, fs, col, th)

    label = f"COUNT {result.total}"
    cv2.rectangle(img, (0, 0), (int(w * 0.32), int(60 * fs * 1.6)), (0, 0, 0), -1)
    cv2.putText(img, label, (10, int(45 * fs * 1.5)), cv2.FONT_HERSHEY_SIMPLEX, fs * 1.6, WHITE, th + 1)
    if extra_text:
        cv2.putText(img, extra_text, (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, fs * 0.8, WHITE, th)
    return img
