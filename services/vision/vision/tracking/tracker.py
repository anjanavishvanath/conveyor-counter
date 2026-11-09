"""
services/vision/vision/tracking/tracker.py

Multi-object tracker in the ByteTrack style, written without external tracking libraries.
Runs on: CPU (numpy + scipy). Cost is tiny compared with detection (well under 1 ms for tens of objects).

How it works, and why it matters for counting:
- Each track has a constant-velocity Kalman filter on (cx, cy, w, h). On a conveyor the motion is
  nearly constant, so the prediction lands close to where the object will be in the next frame,
  even at low frame rates.
- Association happens in two stages (the ByteTrack idea):
    1. confident detections (score >= high_conf) are matched to predicted tracks by IoU;
    2. tracks still unmatched are matched to the LOW-confidence detections.
  Stage 2 keeps a track alive through a blurred or partly hidden frame instead of dropping it and
  starting a new ID later. New IDs are how trackers double-count.
- Only confident detections may start a new track, so a one-off low-score blob can't become a track.
- A lost track coasts on its prediction for max_age_frames before it is deleted.

Conveyor-specific additions (the reason this tracker still works at low frame rates):
- Belt-velocity prior: every object on a belt moves at (nearly) the same velocity. The tracker keeps
  a running estimate of it from confirmed tracks, and NEW tracks start with that velocity instead of
  zero. Without this, at 15 fps a coconut moves ~half its own size per frame, the first prediction
  misses, and the track breaks (tested: 250-coconut video fell from 240 to 137 at 15 fps).
- Centre-distance similarity: when boxes jump far enough that IoU is small, a detection whose centre
  is close to the predicted centre (relative to object size) can still match. Assignment is global
  (Hungarian), so neighbouring objects compete fairly.
- Frame gaps: update(..., steps=n) predicts n frames ahead when the camera dropped frames.
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
from scipy.optimize import linear_sum_assignment

from vision.config import TrackingCfg
from vision.types import Detections, TrackOut


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU between every box in a (N,4) and b (M,4), xyxy."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), np.float32)
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(area_a[:, None] + area_b[None, :] - inter, 1e-6)


def center_similarity(a: np.ndarray, b: np.ndarray, gate: float) -> np.ndarray:
    """1 at identical centres, falling linearly to 0 at distance gate * object size (larger box side)."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), np.float32)
    ca = np.column_stack([(a[:, 0] + a[:, 2]) / 2, (a[:, 1] + a[:, 3]) / 2])
    cb = np.column_stack([(b[:, 0] + b[:, 2]) / 2, (b[:, 1] + b[:, 3]) / 2])
    size = np.maximum(a[:, 2] - a[:, 0], a[:, 3] - a[:, 1])
    d = np.linalg.norm(ca[:, None, :] - cb[None, :, :], axis=2)
    return np.clip(1.0 - d / np.maximum(gate * size[:, None], 1e-6), 0.0, 1.0)


def _xyxy_to_z(box: np.ndarray) -> np.ndarray:
    w, h = box[2] - box[0], box[3] - box[1]
    return np.array([box[0] + w / 2, box[1] + h / 2, w, h], dtype=np.float64)


class _KalmanBox:
    """Constant-velocity Kalman filter. State: cx, cy, w, h, vcx, vcy, vw, vh."""

    F = np.eye(8)
    F[:4, 4:] = np.eye(4)
    H = np.eye(4, 8)

    def __init__(self, box: np.ndarray, velocity: Optional[np.ndarray] = None):
        z = _xyxy_to_z(box)
        v = np.zeros(4)
        if velocity is not None:
            v[:2] = velocity                      # start with the belt's velocity (cx, cy per frame)
        self.x = np.concatenate([z, v])
        s = max(z[2], z[3])
        # Uncertainty scaled to object size, so the filter behaves the same at any resolution.
        # With a belt-velocity prior the initial velocity is fairly well known.
        pv = s * (0.2 if velocity is not None else 1.0)
        self.P = np.diag([s * 0.1, s * 0.1, s * 0.1, s * 0.1, pv, pv, s * 0.1, s * 0.1]) ** 2
        self._s = s

    def predict(self) -> np.ndarray:
        s = max(self.x[2], self.x[3], 1.0)
        q = np.array([0.05, 0.05, 0.05, 0.05, 0.02, 0.02, 0.01, 0.01]) * s
        self.x = self.F @ self.x
        self.x[2:4] = np.maximum(self.x[2:4], 1.0)
        self.P = self.F @ self.P @ self.F.T + np.diag(q ** 2)
        return self.box()

    def update(self, box: np.ndarray) -> None:
        z = _xyxy_to_z(box)
        s = max(z[2], z[3], 1.0)
        R = np.diag((np.array([0.05, 0.05, 0.1, 0.1]) * s) ** 2)
        y = z - self.H @ self.x
        S = self.H @ self.P @ self.H.T + R
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(8) - K @ self.H) @ self.P

    def box(self) -> np.ndarray:
        cx, cy, w, h = self.x[:4]
        return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], dtype=np.float32)


class _Track:
    def __init__(self, track_id: int, box: np.ndarray, score: float, class_id: int,
                 velocity: Optional[np.ndarray] = None):
        self.id = track_id
        self.kf = _KalmanBox(box, velocity)
        self.had_prior = velocity is not None
        self.box = box.astype(np.float32)
        self.score = float(score)
        self.class_id = int(class_id)
        self.hits = 1
        self.age = 0
        self.time_since_update = 0

    def predict(self, steps: int = 1):
        for _ in range(max(1, steps)):
            self.box = self.kf.predict()
        self.age += 1
        self.time_since_update += 1

    @property
    def velocity(self) -> np.ndarray:
        return self.kf.x[4:6].copy()

    def update(self, box: np.ndarray, score: float):
        self.kf.update(box)
        self.box = self.kf.box()
        self.score = float(score)
        self.hits += 1
        self.time_since_update = 0


class Tracker:
    BELT_EMA = 0.2          # smoothing of the belt-velocity estimate
    BELT_MIN_HITS = 2       # tracks with a measured velocity (>= 2 detections) feed the belt estimate

    def __init__(self, cfg: TrackingCfg):
        self.cfg = cfg
        self.tracks: List[_Track] = []
        self._next_id = 1
        self.removed_ids: List[int] = []  # ids deleted during the last update(), for the counter's bookkeeping
        self.belt_velocity: Optional[np.ndarray] = None   # (vx, vy) pixels per frame

    def reset(self) -> None:
        self.tracks.clear()
        self.removed_ids = []
        # IDs keep increasing across resets, so an ID is never reused within a run.
        # The belt-velocity estimate is kept: the belt doesn't change speed between batches.

    UNKNOWN_VELOCITY_GATE = 2.0   # gate multiplier for a 1-hit track with no velocity knowledge yet

    def _similarity(self, tracks: List[_Track], boxes: np.ndarray) -> np.ndarray:
        tb = np.stack([t.box for t in tracks])
        sim = iou_matrix(tb, boxes)
        if self.cfg.center_gate > 0:
            # A track that has only been seen once and started without a belt-velocity prior
            # predicts "no motion", so its gate is widened until it has a velocity of its own.
            gates = np.array([self.cfg.center_gate * (self.UNKNOWN_VELOCITY_GATE
                              if (t.hits == 1 and not t.had_prior) else 1.0) for t in tracks])
            cs = np.vstack([center_similarity(tb[i:i + 1], boxes, g) for i, g in enumerate(gates)])
            sim = np.maximum(sim, cs)
        return sim

    def _match(self, tracks: List[_Track], boxes: np.ndarray):
        if not tracks or len(boxes) == 0:
            return [], list(range(len(tracks))), list(range(len(boxes)))
        sim = self._similarity(tracks, boxes)
        rows, cols = linear_sum_assignment(-sim)
        matches, used_r, used_c = [], set(), set()
        for r, c in zip(rows, cols):
            if sim[r, c] >= self.cfg.match_iou:
                matches.append((r, c))
                used_r.add(r)
                used_c.add(c)
        return (matches,
                [i for i in range(len(tracks)) if i not in used_r],
                [j for j in range(len(boxes)) if j not in used_c])

    def _update_belt_velocity(self) -> None:
        if not self.cfg.belt_velocity_prior:
            return
        vs = [t.velocity for t in self.tracks if t.time_since_update == 0 and t.hits >= self.BELT_MIN_HITS]
        if not vs:
            return
        v = np.median(np.stack(vs), axis=0)
        self.belt_velocity = v if self.belt_velocity is None else \
            (1 - self.BELT_EMA) * self.belt_velocity + self.BELT_EMA * v

    def update(self, dets: Detections, steps: int = 1) -> List[TrackOut]:
        """steps = frames elapsed since the previous update (>1 when the camera dropped frames)."""
        for t in self.tracks:
            t.predict(steps)

        high = dets.scores >= self.cfg.high_conf
        hi_idx, lo_idx = np.where(high)[0], np.where(~high)[0]

        # Stage 1: all tracks vs confident detections
        m1, un_t, un_hi = self._match(self.tracks, dets.boxes[hi_idx])
        for r, c in m1:
            j = hi_idx[c]
            self.tracks[r].update(dets.boxes[j], dets.scores[j])

        # Stage 2: leftover tracks vs low-confidence detections
        left = [self.tracks[i] for i in un_t]
        m2, _, _ = self._match(left, dets.boxes[lo_idx])
        for r, c in m2:
            j = lo_idx[c]
            left[r].update(dets.boxes[j], dets.scores[j])

        # New tracks only from unmatched confident detections
        prior = self.belt_velocity if self.cfg.belt_velocity_prior else None
        for c in un_hi:
            j = hi_idx[c]
            self.tracks.append(_Track(self._next_id, dets.boxes[j], dets.scores[j], dets.class_ids[j], prior))
            self._next_id += 1

        self._update_belt_velocity()

        # Delete tracks lost for too long
        self.removed_ids = [t.id for t in self.tracks if t.time_since_update > self.cfg.max_age_frames]
        self.tracks = [t for t in self.tracks if t.time_since_update <= self.cfg.max_age_frames]

        return [TrackOut(t.id, t.box.copy(), t.score, t.hits, t.age, t.time_since_update == 0)
                for t in self.tracks]
