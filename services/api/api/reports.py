"""
services/api/api/reports.py

Read side of the vision database: production-day summary, batch list, CSV export, and writing the
CSV to a USB stick.

The vision service is the database's only writer. This module opens its own connections with
`PRAGMA query_only=ON`, so a bug here cannot modify production records.
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from vision.store.db import Store, production_day_bounds, production_day_of


class ReadOnlyStore(Store):
    """vision.store.db.Store with read-only connections that never change the journal mode."""

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=self.timeout_s, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("PRAGMA query_only=ON")
            self._local.conn = c
            with self._conns_lock:
                self._conns.append(c)
        return c


class Reports:
    def __init__(self, db_path: Path, site_name: str, tz_name: str, day_start: str):
        self.db_path = Path(db_path)
        self.site_name = site_name
        self.tz = tz_name
        self.day_start = day_start
        self._store: Optional[ReadOnlyStore] = None
        self._lock = threading.Lock()

    # ── helpers ──
    def _db(self) -> Optional[ReadOnlyStore]:
        """None until the vision service has created the database."""
        if not self.db_path.is_file():
            return None
        with self._lock:
            if self._store is None:
                self._store = ReadOnlyStore(self.db_path)
        return self._store

    def today(self) -> date:
        return production_day_of(datetime.now().timestamp(), self.tz, self.day_start)

    def parse_day(self, day: Optional[str]) -> date:
        if not day:
            return self.today()
        try:
            return date.fromisoformat(day)
        except ValueError:
            raise ValueError("day must be YYYY-MM-DD") from None

    # ── queries ──
    def day_report(self, day: date) -> Dict[str, Any]:
        start, end = production_day_bounds(day, self.tz, self.day_start)
        empty = {"day": day.isoformat(), "start": start, "end": end, "timezone": self.tz,
                 "day_start": self.day_start, "batches": 0, "counted": 0, "targeted": 0,
                 "uncertain_batches": 0, "corrections": 0, "per_crate": []}
        db = self._db()
        if db is None:
            return {"summary": empty, "batches": [], "db_ready": False}
        try:
            summary = db.day_summary(day, self.tz, self.day_start)
            batches = db.list_batches(since=start, until=end, limit=2000)
        except sqlite3.OperationalError as e:  # vision has not migrated the schema yet
            return {"summary": empty, "batches": [], "db_ready": False, "error": str(e)}
        for b in batches:
            corr = sum(c["delta"] for c in db.corrections_for(b["batch_id"]))
            b["corrections"] = corr
            b["corrected_count"] = b["count"] + corr
            b["duration_s"] = (round(b["completed_at"] - b["started_at"], 1)
                               if b.get("started_at") and b.get("completed_at") else None)
            for k in ("product_profile",):
                b.pop(k, None)
        return {"summary": summary, "batches": batches, "db_ready": True}

    def last_completed_per_crate(self, day: date) -> Dict[int, Dict[str, Any]]:
        """Latest finished batch of each crate this production day (for the crate tiles)."""
        start, end = production_day_bounds(day, self.tz, self.day_start)
        db = self._db()
        if db is None:
            return {}
        out: Dict[int, Dict[str, Any]] = {}
        try:
            for b in db.list_batches(since=start, until=end, status="completed", limit=2000):
                out.setdefault(int(b["crate"]), {"count": b["count"], "target": b["target"],
                                                 "completed_at": b["completed_at"],
                                                 "uncertain": b["uncertain"]})
        except sqlite3.OperationalError:
            return {}
        return out

    # ── CSV ──
    def csv_rows(self, day: date) -> Iterable[List[Any]]:
        start, end = production_day_bounds(day, self.tz, self.day_start)
        db = self._db()
        if db is None:
            return []
        return db.export_rows(start, end, self.tz, self.day_start)

    def csv_text(self, day: date) -> Tuple[str, int]:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(Store.CSV_HEADER)
        n = 0
        for row in self.csv_rows(day):
            w.writerow(row)
            n += 1
        return buf.getvalue(), n

    def csv_filename(self, day: date) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", self.site_name).strip("-").lower() or "counter"
        stamp = datetime.now().strftime("%H%M%S")
        return f"{slug}_{day.isoformat()}_{stamp}.csv"


# ───────────────────────── USB export ─────────────────────────
def find_usb_mounts(roots: List[str]) -> List[Path]:
    """
    Writable removable-media mount points (Linux desktop auto-mount: /media/<user>/<label> or
    /run/media/<user>/<label>). Empty on Windows or when no stick is inserted.
    """
    found: List[Path] = []
    for root in roots:
        r = Path(root)
        if not r.is_dir():
            continue
        candidates = []
        try:
            for p in r.iterdir():
                candidates.append(p)
                if p.is_dir() and not os.path.ismount(p):
                    candidates.extend(q for q in p.iterdir() if q.is_dir())
        except OSError:
            continue
        for p in candidates:
            try:
                if p.is_dir() and os.path.ismount(p) and os.access(p, os.W_OK):
                    found.append(p)
            except OSError:
                pass
    return sorted(set(found))


def write_csv_file(directory: Path, filename: str, text: str) -> Path:
    """Write and fsync, so the stick can be pulled as soon as the UI says it is saved."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if hasattr(os, "sync"):
        os.sync()
    return path
