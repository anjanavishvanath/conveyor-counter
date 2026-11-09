"""
services/vision/vision/detectors/replay.py

DEV ONLY. Replays detections saved by `tools/eval/count_eval.py --save-dets` instead of running
the model, so the vision service, the API and the UI can be exercised at the camera's full frame
rate on a PC whose CPU cannot run the model that fast.

Runs on: nothing (no inference at all). The config loader refuses it unless the frame source is a
video file and the IO backend is the simulator, so it can never drive a real conveyor.

Frames are matched by their position in the file (Frame.timestamp = index / fps), so looping and
dropped frames are handled. A frame missing from the cache yields no detections and is counted in
`misses`, which is logged: a cache made from a different video shows up immediately.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Tuple

import numpy as np

from vision.detectors.base import Detector
from vision.types import Detections

log = logging.getLogger("replay")


class ReplayDetector(Detector):
    def __init__(self, dets_file: str | Path):
        path = Path(dets_file)
        if not path.is_file():
            raise FileNotFoundError(
                f"Replay cache not found: {path}\n"
                f"  make it with: python tools/eval/count_eval.py --save-dets {path}")
        z = np.load(path, allow_pickle=False)
        if int(z["stride"]) != 1:
            raise ValueError(f"{path} was saved with --stride {int(z['stride'])}; replay needs --stride 1")
        self._boxes = z["boxes"].astype(np.float32)
        self._scores = z["scores"].astype(np.float32)
        self._class_ids = z["class_ids"].astype(np.int32)
        offs = np.concatenate([[0], np.cumsum(z["lens"])]).astype(np.int64)
        self._index: Dict[int, Tuple[int, int]] = {
            self._key(float(ts)): (int(offs[i]), int(offs[i + 1])) for i, ts in enumerate(z["timestamps"])
        }
        self._frame_size = tuple(int(v) for v in z["frame_size"])
        self.hits = 0
        self.misses = 0
        self._size_checked = False
        self._t: Dict[str, float] = {}
        log.warning("REPLAY detector (dev only): %d frames from %s, recorded with model %s",
                    len(self._index), path.name, Path(str(z["model_file"])).name)

    @staticmethod
    def _key(timestamp: float) -> int:
        return int(round(timestamp * 1000.0))

    @property
    def device(self) -> str:
        return "replay"

    @property
    def last_timings_ms(self) -> Dict[str, float]:
        return self._t

    def detect(self, image_bgr: np.ndarray) -> Detections:
        raise RuntimeError("ReplayDetector matches frames by position: call detect_frame(frame)")

    def detect_frame(self, frame) -> Detections:
        if not self._size_checked:
            self._size_checked = True
            h, w = frame.image.shape[:2]
            if (w, h) != self._frame_size:
                log.error("replay cache was made at %dx%d but the video is %dx%d: wrong video?",
                          self._frame_size[0], self._frame_size[1], w, h)
        span = self._index.get(self._key(frame.timestamp))
        self._t = {"inference": 0.0}
        if span is None:
            self.misses += 1
            if self.misses in (1, 10, 100) or self.misses % 1000 == 0:
                log.error("replay cache has no entry for frame at t=%.3f s (%d misses)", frame.timestamp, self.misses)
            return Detections.empty()
        self.hits += 1
        a, b = span
        return Detections(self._boxes[a:b], self._scores[a:b], self._class_ids[a:b])
