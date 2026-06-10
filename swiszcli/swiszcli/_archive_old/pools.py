"""Persistent scored choice pools for pick_or_new step kind.

A pool is a named bag of (value,label) entries with usage stats. Wizards
draw their top-N options from a pool, plus a sentinel "+ new (type one)"
row. When the user/LLM picks "new", the freshly typed value is APPENDED
to the pool, so next time it competes for a top-N slot.

This is the brick the memory palace is built from: every new pathway the
LLM walks gets persisted; recurring pathways rise to the top by usage.

Storage: sqlite at <state_dir>/pools.db. Schema is idempotent.
Ranking: use_count * recency_decay (half-life 14d).

NO fallbacks. NO silent eviction. UNIQUE(pool, value) — duplicates upsert.
"""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_entries (
    id          INTEGER PRIMARY KEY,
    pool        TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    use_count   INTEGER NOT NULL DEFAULT 0,
    last_used   REAL,
    created_by  TEXT    NOT NULL DEFAULT \'user\',  -- user | llm | seed
    created_at  REAL    NOT NULL,
    UNIQUE(pool, value)
);
CREATE INDEX IF NOT EXISTS idx_pool_entries_pool ON pool_entries(pool);
"""

# Half-life in seconds for recency weighting (14 days).
HALF_LIFE_S = 14 * 24 * 3600.0


@dataclass(frozen=True)
class PoolEntry:
    id: int
    pool: str
    value: str
    label: str
    use_count: int
    last_used: float | None
    created_by: str
    created_at: float

    def score(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        if self.last_used is None:
            recency = 0.5  # never used: middle weight, lets new things compete
        else:
            age = max(0.0, now - self.last_used)
            recency = math.pow(0.5, age / HALF_LIFE_S)
        # +1 so unused-but-seeded entries rank above pure age-zero junk
        return (self.use_count + 1) * recency


class ChoicePool:
    """A named pool of choices, backed by sqlite. Pools are created lazily."""

    def __init__(self, db_path: Path, pool: str) -> None:
        self.db_path = Path(db_path)
        self.pool = pool
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── reads ────────────────────────────────────────────────────────────
    def all(self) -> list[PoolEntry]:
        rows = self._conn.execute(
            "SELECT * FROM pool_entries WHERE pool = ? ORDER BY id",
            (self.pool,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def top(self, n: int = 10) -> list[PoolEntry]:
        entries = self.all()
        entries.sort(key=lambda e: e.score(), reverse=True)
        return entries[:n]

    def find(self, value: str) -> PoolEntry | None:
        row = self._conn.execute(
            "SELECT * FROM pool_entries WHERE pool = ? AND value = ?",
            (self.pool, value),
        ).fetchone()
        return self._row(row) if row else None

    # ── writes ───────────────────────────────────────────────────────────
    def add(self, value: str, label: str | None = None, *, created_by: str = "user") -> PoolEntry:
        """UPSERT: re-adding an existing value is a no-op (returns existing)."""
        existing = self.find(value)
        if existing:
            return existing
        now = time.time()
        cur = self._conn.execute(
            """INSERT INTO pool_entries (pool, value, label, use_count,
               last_used, created_by, created_at)
               VALUES (?, ?, ?, 0, NULL, ?, ?)""",
            (self.pool, value, label or value, created_by, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM pool_entries WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return self._row(row)

    def touch(self, value: str) -> None:
        """Increment use_count + bump last_used. Fail loud if value missing."""
        now = time.time()
        cur = self._conn.execute(
            """UPDATE pool_entries SET use_count = use_count + 1, last_used = ?
               WHERE pool = ? AND value = ?""",
            (now, self.pool, value),
        )
        if cur.rowcount == 0:
            raise KeyError(f"pool {self.pool!r} has no value {value!r} to touch")
        self._conn.commit()

    def seed(self, entries: Iterable[tuple[str, str]]) -> None:
        """Bulk-seed (value,label) pairs as created_by=seed. Idempotent."""
        for value, label in entries:
            self.add(value, label, created_by="seed")

    def remove(self, value: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM pool_entries WHERE pool = ? AND value = ?",
            (self.pool, value),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _row(r: sqlite3.Row) -> PoolEntry:
        return PoolEntry(
            id=r["id"], pool=r["pool"], value=r["value"], label=r["label"],
            use_count=r["use_count"], last_used=r["last_used"],
            created_by=r["created_by"], created_at=r["created_at"],
        )


# ── module-level convenience: a singleton db per process ────────────────

_DEFAULT_DB: Path | None = None


def set_default_db(path: Path) -> None:
    global _DEFAULT_DB
    _DEFAULT_DB = Path(path)


def get_pool(name: str) -> ChoicePool:
    if _DEFAULT_DB is None:
        raise RuntimeError("pools.set_default_db(path) was never called")
    return ChoicePool(_DEFAULT_DB, name)
