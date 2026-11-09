"""
services/vision/vision/sources/video_file.py

Plays a recorded video as if it were the camera (PC development and offline evaluation).
- realtime=True paces frames at the file's fps and DROPS frames if processing falls behind,
  exactly like a live camera would. Dropped frames show up as frame_id gaps.
- realtime=False returns every frame as fast as it can be processed (offline evaluation).
- stride=N returns every Nth frame, to simulate a lower camera/processing frame rate.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import cv2

from vision.sources.base import FrameSource, SourceError
from vision.types import Frame


class VideoFileSource(FrameSource):
    def __init__(self, path: str | Path, realtime: bool = True, loop: bool = False, stride: int = 1):
        self.path = Path(path)
        self.realtime = realtime
        self.loop = loop
        self.stride = max(1, int(stride))
        self._cap: Optional[cv2.VideoCapture] = None
        self._fps = 0.0
        self._next_idx = 0
        self._t0 = 0.0
        self._loop_offset = 0  # keeps frame_id increasing across loops

    @property
    def nominal_fps(self) -> float:
        return self._fps / self.stride if self._fps else 0.0

    @property
    def frame_count(self) -> int:
        return int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT)) if self._cap else 0

    def open(self) -> None:
        if not self.path.is_file():
            raise SourceError(f"Video file not found: {self.path}")
        self._cap = cv2.VideoCapture(str(self.path))
        if not self._cap.isOpened():
            raise SourceError(f"Could not open video: {self.path}")
        self._fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._next_idx = 0
        self._t0 = time.monotonic()

    def _grab_to(self, idx: int) -> bool:
        """Advance the file to frame index idx (grab without decoding the skipped frames)."""
        while self._next_idx < idx:
            if not self._cap.grab():
                return False
            self._next_idx += 1
        return True

    def read(self, timeout_s: float = 1.0) -> Optional[Frame]:
        if self._cap is None:
            raise SourceError("Source not opened")

        target = self._next_idx
        if self.realtime:
            # Which frame "is the camera showing now"? Skip anything we were too slow for.
            due = int((time.monotonic() - self._t0) * self._fps)
            if due > target:
                target = due
            else:
                time.sleep(max(0.0, (target / self._fps) - (time.monotonic() - self._t0)))
        # Stride: only frames whose index is a multiple of stride are delivered
        if target % self.stride:
            target += self.stride - (target % self.stride)

        if not self._grab_to(target):
            return self._end()
        ok, img = self._cap.read()
        if not ok:
            return self._end()
        idx = self._next_idx
        self._next_idx += 1
        return Frame(image=img, frame_id=self._loop_offset + idx, timestamp=idx / self._fps)

    def _end(self) -> Optional[Frame]:
        if self.loop:
            self._loop_offset += self._next_idx
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self._next_idx = 0
            self._t0 = time.monotonic()
            return self.read()
        return None

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
