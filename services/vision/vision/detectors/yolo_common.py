"""
services/vision/vision/detectors/yolo_common.py

Pre- and post-processing for Ultralytics YOLOv8/YOLO11 detection models exported WITHOUT NMS
(output shape (1, 4 + num_classes, N), boxes as cx, cy, w, h in model-input pixels).
Shared by every runtime backend (ONNX CPU now, NPU later), so all backends give identical boxes.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from vision.types import Detections


@dataclass
class Letterbox:
    scale: float
    pad_x: float
    pad_y: float


def letterbox(image_bgr: np.ndarray, width: int, height: int, pad_value: int = 114):
    """Resize keeping aspect ratio and pad to (height, width) — same as Ultralytics training."""
    h, w = image_bgr.shape[:2]
    scale = min(width / w, height / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(image_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR) if (nw, nh) != (w, h) else image_bgr
    pad_x, pad_y = (width - nw) / 2, (height - nh) / 2
    top, left = int(round(pad_y - 0.1)), int(round(pad_x - 0.1))
    out = cv2.copyMakeBorder(resized, top, height - nh - top, left, width - nw - left,
                             cv2.BORDER_CONSTANT, value=(pad_value,) * 3)
    return out, Letterbox(scale, left, top)


def to_nchw_float(image_bgr: np.ndarray) -> np.ndarray:
    """BGR uint8 HxWx3 -> RGB float32 1x3xHxW in [0, 1]."""
    rgb = image_bgr[:, :, ::-1]
    return np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0


def decode(output: np.ndarray, lb: Letterbox, frame_w: int, frame_h: int,
           conf_threshold: float, nms_iou: float, max_detections: int) -> Detections:
    """Raw YOLO output (1, 4+nc, N) or (4+nc, N) -> NMS-filtered Detections in frame pixels."""
    out = np.asarray(output, dtype=np.float32)
    if out.ndim == 3:
        out = out[0]
    if out.shape[0] > out.shape[1]:  # tolerate (N, 4+nc) layouts from other exporters
        out = out.T
    boxes_cxcywh, cls_scores = out[:4].T, out[4:].T
    class_ids = cls_scores.argmax(axis=1)
    scores = cls_scores[np.arange(len(class_ids)), class_ids]
    keep = scores >= conf_threshold
    if not keep.any():
        return Detections.empty()
    b, scores, class_ids = boxes_cxcywh[keep], scores[keep], class_ids[keep]

    # cx,cy,w,h (model input) -> x1,y1,x2,y2 (frame)
    xyxy = np.empty_like(b)
    xyxy[:, 0] = (b[:, 0] - b[:, 2] / 2 - lb.pad_x) / lb.scale
    xyxy[:, 1] = (b[:, 1] - b[:, 3] / 2 - lb.pad_y) / lb.scale
    xyxy[:, 2] = (b[:, 0] + b[:, 2] / 2 - lb.pad_x) / lb.scale
    xyxy[:, 3] = (b[:, 1] + b[:, 3] / 2 - lb.pad_y) / lb.scale
    xyxy[:, [0, 2]] = xyxy[:, [0, 2]].clip(0, frame_w)
    xyxy[:, [1, 3]] = xyxy[:, [1, 3]].clip(0, frame_h)

    # Class-aware NMS (offset boxes per class so different classes never suppress each other)
    offset = class_ids[:, None].astype(np.float32) * (max(frame_w, frame_h) + 1)
    nms_boxes = xyxy + offset
    xywh = np.column_stack([nms_boxes[:, 0], nms_boxes[:, 1],
                            nms_boxes[:, 2] - nms_boxes[:, 0], nms_boxes[:, 3] - nms_boxes[:, 1]])
    idx = cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), conf_threshold, nms_iou, top_k=max_detections)
    idx = np.array(idx, dtype=np.int64).reshape(-1)
    return Detections(xyxy[idx].astype(np.float32), scores[idx].astype(np.float32), class_ids[idx].astype(np.int32))
