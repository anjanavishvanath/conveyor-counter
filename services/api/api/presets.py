"""
services/api/api/presets.py

Per-crate target presets (the "preset value" on each crate tile). Owned by the API, stored in its
own small SQLite file (`api.settings_db`), so the vision database keeps a single writer.

A preset is only a default. The target that counts is the one sent with `select_batch` and stored
on the batch row by the vision service, so changing a preset never changes a running batch.
Every change is logged with the old and new value.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crate_presets (
    crate      INTEGER PRIMARY KEY,
    target     INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS preset_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    crate   INTEGER NOT NULL,
    old     INTEGER,
    new     INTEGER NOT NULL,
    source  TEXT NOT NULL DEFAULT ''
);
"""


class PresetError(ValueError):
    pass


class PresetStore:
    def __init__(self, path: Path, crates: int, default_target: int, max_target: int):
        self.path = Path(path)
        self.crates = crates
        self.default_target = default_target
        self.max_target = max_target
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        """One short-lived connection per call: commit on success, roll back on error, always close."""
        c = sqlite3.connect(str(self.path), timeout=5.0)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            with c:
                yield c
        finally:
            c.close()

    def _check(self, crate: Optional[int], target: int) -> None:
        if crate is not None and not 1 <= crate <= self.crates:
            raise PresetError(f"crate must be 1..{self.crates}")
        if not 1 <= int(target) <= self.max_target:
            raise PresetError(f"target must be 1..{self.max_target}")

    def all(self) -> List[Dict[str, int]]:
        with self._lock, self._connect() as c:
            rows = dict(c.execute("SELECT crate, target FROM crate_presets").fetchall())
        return [{"crate": n, "target": int(rows.get(n, self.default_target))} for n in range(1, self.crates + 1)]

    def get(self, crate: int) -> int:
        self._check(crate, 1)
        with self._lock, self._connect() as c:
            row = c.execute("SELECT target FROM crate_presets WHERE crate=?", (crate,)).fetchone()
        return int(row[0]) if row else self.default_target

    def set(self, crate: int, target: int, source: str = "ui") -> None:
        self._check(crate, target)
        self._write({crate: int(target)}, source)

    def set_all(self, target: int, source: str = "ui") -> None:
        self._check(None, target)
        self._write({n: int(target) for n in range(1, self.crates + 1)}, source)

    def _write(self, values: Dict[int, int], source: str) -> None:
        now = time.time()
        with self._lock, self._connect() as c:  # one transaction for all crates
            old = dict(c.execute("SELECT crate, target FROM crate_presets").fetchall())
            for crate, target in values.items():
                prev = old.get(crate, self.default_target)
                c.execute("INSERT INTO crate_presets (crate, target, updated_at) VALUES (?, ?, ?) "
                          "ON CONFLICT(crate) DO UPDATE SET target=excluded.target, updated_at=excluded.updated_at",
                          (crate, target, now))
                if prev != target:
                    c.execute("INSERT INTO preset_log (ts, crate, old, new, source) VALUES (?, ?, ?, ?, ?)",
                              (now, crate, prev, target, source))
