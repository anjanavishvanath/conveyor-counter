"""
services/vision/tests/test_counting.py — checkpoint 2 unit tests

Synthetic scenes covering the ways a conveyor counter goes wrong:
missed frames, flicker at the line, low-confidence frames, objects appearing past the line,
touching objects side by side, low frame rate, and false one-frame detections.
"""
import numpy as np
import pytest

from tests.synth import H, W, moving_objects
from vision.config import CountingCfg, TrackingCfg
from vision.counting.line_counter import LineCounter
from vision.detectors.yolo_common import Letterbox, decode, letterbox
from vision.tracking.tracker import Tracker
from vision.types import Detections

TRACK = TrackingCfg(high_conf=0.5, match_iou=0.2, max_age_frames=10, min_hits=3)
COUNT = CountingCfg(axis="y", line_position=0.5, direction="positive", hysteresis=0.03, min_track_hits=3)


def run(frames, tracking=TRACK, counting=COUNT):
    tr, lc = Tracker(tracking), LineCounter(counting)
    for i, d in enumerate(frames):
        tracks = tr.update(d)
        lc.update(tracks, tr.removed_ids, W, H, i, i / 30)
    # flush: let every remaining track expire so end-of-run statistics are complete
    for j in range(tracking.max_age_frames + 2):
        lc.update(tr.update(Detections.empty()), tr.removed_ids, W, H, len(frames) + j, 0)
    return lc.stats


# ─── counting correctness ───────────────────────────────────────────────────
def test_single_stream_exact():
    assert run(moving_objects(50, spacing_frames=12, speed_px=15)).total == 50


def test_three_lanes_touching_side_by_side():
    # three objects abreast, 125 px apart centre-to-centre with 120 px boxes: nearly touching
    frames = moving_objects(60, spacing_frames=4, speed_px=12, x_positions=[515, 640, 765])
    assert run(frames).total == 60


def test_low_fps_large_steps():
    # 45 px per frame ~ what 10 fps looks like on a belt that moves 15 px/frame at 30 fps
    assert run(moving_objects(40, spacing_frames=6, speed_px=45)).total == 40


def test_fast_motion_needs_belt_velocity_prior():
    # 100 px/frame with 120 px objects: consecutive boxes barely overlap (IoU ~0.09, below match_iou
    # 0.2). Only the belt-velocity prior + centre gate keep tracks together.
    frames = moving_objects(40, spacing_frames=4, speed_px=100)
    assert run(frames).total == 40
    no_prior = TrackingCfg(**{**TRACK.model_dump(), "belt_velocity_prior": False, "center_gate": 0.0})
    assert run(frames, tracking=no_prior).total < 40     # proves the test exercises the prior


def test_missed_detections_do_not_double_count():
    frames = moving_objects(30, spacing_frames=15, speed_px=15)
    rng = np.random.default_rng(0)
    for i in range(len(frames)):
        if rng.random() < 0.25:  # detector misses 25 % of frames at random
            frames[i] = Detections.empty()
    assert run(frames).total == 30


def test_low_confidence_frames_keep_track_alive():
    frames = moving_objects(20, spacing_frames=20, speed_px=15)
    for i in range(0, len(frames), 3):  # every 3rd frame the score drops below high_conf (blur)
        d = frames[i]
        frames[i] = Detections(d.boxes, np.full(len(d), 0.3, np.float32), d.class_ids)
    stats = run(frames)
    assert stats.total == 20


def test_jitter_on_the_line_counts_once():
    frames = []
    for f in range(60):  # object parked on the line, jittering +-20 px
        cy = H / 2 + (20 if f % 2 else -20) + (0 if f < 5 else 0)
        frames.append(Detections(np.array([[580, cy - 60, 700, cy + 60]], np.float32),
                                 np.array([0.9], np.float32), np.array([0], np.int32)))
    # then it moves off downstream
    for k in range(20):
        cy = H / 2 + 20 + k * 20
        frames.append(Detections(np.array([[580, cy - 60, 700, cy + 60]], np.float32),
                                 np.array([0.9], np.float32), np.array([0], np.int32)))
    # born inside the dead band then leaves downstream -> exactly one count
    assert run(frames).total == 1


def test_object_first_seen_downstream_not_counted_but_flagged():
    frames = moving_objects(1, spacing_frames=1, speed_px=15, start_y=H * 0.6)
    stats = run(frames)
    assert stats.total == 0
    assert stats.born_downstream == 1


def test_one_frame_false_detection_not_counted():
    frames = [Detections.empty() for _ in range(10)]
    frames[5] = Detections(np.array([[600, 400, 700, 500]], np.float32), np.array([0.95], np.float32),
                           np.array([0], np.int32))
    assert run(frames).total == 0


def test_negative_direction_and_x_axis():
    # objects moving right-to-left: swap axes by transposing the synthetic scene
    base = moving_objects(25, spacing_frames=12, speed_px=15)
    frames = []
    for d in base:
        b = d.boxes.copy()
        # map y (0..H, downward) to x (W..0, leftward)
        x1 = W - b[:, 3] * W / H
        x2 = W - b[:, 1] * W / H
        nb = np.column_stack([x1, b[:, 0] * H / W, x2, b[:, 2] * H / W]).astype(np.float32)
        frames.append(Detections(nb, d.scores, d.class_ids))
    cfg = CountingCfg(axis="x", line_position=0.5, direction="negative", hysteresis=0.03, min_track_hits=3)
    assert run(frames, counting=cfg).total == 25


# ─── post-processing ────────────────────────────────────────────────────────
def test_letterbox_and_decode_roundtrip():
    img = np.zeros((720, 1280, 3), np.uint8)
    lb_img, lb = letterbox(img, 640, 640)
    assert lb_img.shape == (640, 640, 3)
    # one object at frame box (100,200)-(300,400) -> model coords
    fb = np.array([100, 200, 300, 400], np.float32)
    mb = fb * lb.scale + np.array([lb.pad_x, lb.pad_y, lb.pad_x, lb.pad_y])
    cx, cy, w, h = (mb[0] + mb[2]) / 2, (mb[1] + mb[3]) / 2, mb[2] - mb[0], mb[3] - mb[1]
    out = np.zeros((1, 5, 8400), np.float32)
    out[0, :, 0] = [cx, cy, w, h, 0.9]
    out[0, :, 1] = [cx + 1, cy, w, h, 0.8]  # duplicate -> removed by NMS
    dets = decode(out, lb, 1280, 720, conf_threshold=0.25, nms_iou=0.5, max_detections=100)
    assert len(dets) == 1
    assert np.allclose(dets.boxes[0], fb, atol=1.0)
    assert dets.scores[0] == pytest.approx(0.9)


def test_decode_empty():
    out = np.zeros((1, 5, 8400), np.float32)
    assert len(decode(out, Letterbox(0.5, 0, 140), 1280, 720, 0.25, 0.5, 100)) == 0
