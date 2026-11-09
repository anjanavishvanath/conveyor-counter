"""services/vision/vision/sources/base.py — frame source interface (video file on PC, Hikrobot camera on board)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from vision.types import Frame


class SourceError(RuntimeError):
    """Raised when the source is broken (camera unplugged, file unreadable). Treated as a FAULT upstream."""


class FrameSource(ABC):
    @abstractmethod
    def open(self) -> None:
        """Open the device/file. Raise SourceError on failure."""

    @abstractmethod
    def read(self, timeout_s: float = 1.0) -> Optional[Frame]:
        """
        Return the next frame, or None at a clean end of stream (video file finished).
        Raise SourceError if no frame arrives within timeout_s or the device fails.
        """

    @abstractmethod
    def close(self) -> None: ...

    @property
    def nominal_fps(self) -> float:
        return 0.0

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
