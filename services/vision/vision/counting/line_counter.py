"""
services/vision/vision/counting/line_counter.py

Counts tracked objects when their centre CROSSES a line across the conveyor.

Rules (fixes for the Pi project's "centre is past the line and ID not seen" rule):
- A track is counted once, when its centre moves from the upstream side of the line to the
  downstream side. A track that first appears already downstream is NOT counted (it would be a
  re-appearing object with a new ID) but is reported as a suspect.
- A dead band (hysteresis) around the line stops a box that jitters on the line from flipping sides.
- A track must have at least min_track_hits real detections before it can count, so a short-lived
  false detection can't add to the count.
- Only real detections move a track across the line. A track coasting on its prediction (no
  detection this frame) can't be counted, so the tracker can't "invent" a coconut.
- A track first seen inside the dead band that then leaves on the downstream side IS counted
  (the object really did pass the line; it was just detected late).

Suspect statistics are kept so a miscount is never silent:
- born_downstream: confirmed tracks that first appeared past the line (possible missed count)
- lost_upstream_near_line: tracks deleted while upstream but close to the line (possible missed count)
- backward_crossings: counted tracks that moved back across the line (possible double count if they return)
- crossed_unconfirmed: tracks that crossed but were deleted before min_track_hits (possible missed count
  or a filtered-out false detection)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import numpy as np

from vision.config import CountingCfg
from vision.types import CountEvent, TrackOut

UPSTREAM, BAND, DOWNSTREAM = -1, 0, 1


@dataclass
class _TrackState:
    side: int                 # last definite side (UPSTREAM / DOWNSTREAM), BAND if never left the band
    born_side: int
    hits: int = 0
    counted: bool = False
    crossed: bool = False     # crossed the line but not yet confirmed (hits < min_track_hits)
    last_pos: float = 0.0
    flagged_born_downstream: bool = False


@dataclass
class CounterStats:
    total: int = 0
    born_downstream: int = 0
    lost_upstream_near_line: int = 0
    backward_crossings: int = 0
    crossed_unconfirmed: int = 0
    counted_ids: List[int] = field(default_factory=list)

    def as_dict(self) -> Dict[str, int]:
        return {"total": self.total, "born_downstream": self.born_downstream,
                "lost_upstream_near_line": self.lost_upstream_near_line,
                "backward_crossings": self.backward_crossings,
                "crossed_unconfirmed": self.crossed_unconfirmed}

    @property
    def suspects(self) -> int:
        """Events that may mean a miscount. Non-zero doesn't prove an error; it means 'look here'."""
        return self.born_downstream + self.lost_upstream_near_line + self.backward_crossings + self.crossed_unconfirmed


class LineCounter:
    NEAR_LINE = 0.15  # "close to the line" for the lost-track suspect statistic (fraction of frame)

    def __init__(self, cfg: CountingCfg, count_class_ids=None):
        self.cfg = cfg
        self.count_class_ids = set(count_class_ids) if count_class_ids is not None else None
        self._states: Dict[int, _TrackState] = {}
        self.stats = CounterStats()

    def reset(self) -> None:
        self._states.clear()
        self.stats = CounterStats()

    def _pos(self, box: np.ndarray, frame_w: int, frame_h: int) -> float:
        """Normalized position along the motion axis, flipped so 'downstream' is always increasing."""
        if self.cfg.axis == "y":
            p = (box[1] + box[3]) / 2 / frame_h
        else:
            p = (box[0] + box[2]) / 2 / frame_w
        return p if self.cfg.direction == "positive" else 1.0 - p

    @property
    def _line(self) -> float:
        lp = self.cfg.line_position
        return lp if self.cfg.direction == "positive" else 1.0 - lp

    def _side(self, pos: float) -> int:
        if pos < self._line - self.cfg.hysteresis:
            return UPSTREAM
        if pos > self._line + self.cfg.hysteresis:
            return DOWNSTREAM
        return BAND

    def update(self, tracks: List[TrackOut], removed_ids: List[int], frame_w: int, frame_h: int,
               frame_id: int, timestamp: float) -> List[CountEvent]:
        events: List[CountEvent] = []

        for t in tracks:
            if not t.updated:
                continue  # coasting tracks never change side
            pos = self._pos(t.box, frame_w, frame_h)
            side = self._side(pos)
            st = self._states.get(t.track_id)
            if st is None:
                st = _TrackState(side=side, born_side=side)
                self._states[t.track_id] = st
            st.hits = t.hits
            st.last_pos = pos

            if side == DOWNSTREAM and not st.counted:
                if st.born_side != DOWNSTREAM and st.side in (UPSTREAM, BAND):
                    st.crossed = True  # crossed; counts as soon as the track is confirmed
                if st.crossed and t.hits >= self.cfg.min_track_hits:
                    st.counted = True
                    self.stats.total += 1
                    self.stats.counted_ids.append(t.track_id)
                    events.append(CountEvent(t.track_id, frame_id, timestamp))
                elif st.born_side == DOWNSTREAM and t.hits >= self.cfg.min_track_hits and not st.flagged_born_downstream:
                    st.flagged_born_downstream = True
                    self.stats.born_downstream += 1
            elif side == UPSTREAM and st.counted and st.side == DOWNSTREAM:
                self.stats.backward_crossings += 1

            if side != BAND:
                st.side = side

        for tid in removed_ids:
            st = self._states.pop(tid, None)
            if st is None or st.counted:
                continue
            if st.crossed:
                self.stats.crossed_unconfirmed += 1
            elif (st.side in (UPSTREAM, BAND) and st.hits >= self.cfg.min_track_hits
                    and self._line - st.last_pos < self.NEAR_LINE):
                self.stats.lost_upstream_near_line += 1

        return events
