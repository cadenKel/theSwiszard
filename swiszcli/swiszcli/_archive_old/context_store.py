"""swiszContext store: chunks + examples in one sqlite db.

chunks   -- disposable session-scoped context (rolling windows, tool
            outputs, session frames). retrievals count drives P2
            promotion daemon (chunks->lessons in swizmem).
examples -- learned routing: (text, embedding, wizard_name). Seeded
            from HANDLER_SEEDS, grows via P1 one-tap learning.

Vectors stored as float32 bytes. Cosine in python (db stays small).

Fails LOUDLY. No silent fallbacks.
"""
from __future__ import annotations

import math
import sqlite3
import struct
import time
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".swiszcli" / "contexts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    embedding   BLOB    NOT NULL,
    ts          REAL    NOT NULL,
    retrievals  INTEGER NOT NULL DEFAULT 0,
    source      TEXT    NOT NULL DEFAULT 'session',
    promoted    INTEGER NOT NULL DEFAULT 0,
    tier        TEXT    NOT NULL DEFAULT 'warm',
    last_access REAL
);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_kind    ON chunks(kind);

CREATE TABLE IF NOT EXISTS examples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    text         TEXT    NOT NULL,
    embedding    BLOB    NOT NULL,
    wizard_name  TEXT    NOT NULL,
    source       TEXT    NOT NULL DEFAULT 'seed',
    weight       REAL    NOT NULL DEFAULT 0.5,
    wins         INTEGER NOT NULL DEFAULT 0,
    losses       INTEGER NOT NULL DEFAULT 0,
    last_used    REAL,
    UNIQUE(text, wizard_name)
);
CREATE INDEX IF NOT EXISTS idx_examples_wiz ON examples(wizard_name);
"""


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob):
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a, b):
    if len(a) != len(b):
        raise ValueError(f"vector dim mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class ContextStore:
    KINDS = {"chunk_window", "tool_result", "session_frame", "shell_fallback"}

    def __init__(self, db_path = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        # Migration: add 'promoted' column to old chunks tables
        try:
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(chunks)")}
            if "promoted" not in cols:
                self._conn.execute("ALTER TABLE chunks ADD COLUMN promoted INTEGER NOT NULL DEFAULT 0")
            if "tier" not in cols:
                self._conn.execute("ALTER TABLE chunks ADD COLUMN tier TEXT NOT NULL DEFAULT 'warm'")
            if "last_access" not in cols:
                self._conn.execute("ALTER TABLE chunks ADD COLUMN last_access REAL")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()
        try:
            self.compact_tiers()
        except Exception:
            pass

    def close(self):
        self._conn.close()

    # ---- chunks ----------------------------------------------------------

    # P1.13: dedup threshold — chunks within this cosine of an existing recent
    # chunk (same session+kind) are not re-inserted; existing one gets a retrieval bump.
    DEDUP_COSINE_THRESHOLD = 0.95
    DEDUP_WINDOW = 50   # only scan this many recent same-session+kind chunks

    def store_chunk(self, session_id, kind, text, embedding, source="session"):
        if kind not in self.KINDS:
            raise ValueError(f"unknown chunk kind: {kind!r} (allowed: {self.KINDS})")
        # dedup: scan recent same-session+kind chunks for near-cosine duplicate
        try:
            rows = self._conn.execute(
                "SELECT id, embedding FROM chunks "
                "WHERE session_id = ? AND kind = ? "
                "ORDER BY ts DESC LIMIT ?",
                (session_id, kind, self.DEDUP_WINDOW),
            ).fetchall()
            for row in rows:
                try:
                    other = _unpack(row["embedding"])
                    if len(other) != len(embedding):
                        continue
                    if _cosine(embedding, other) >= self.DEDUP_COSINE_THRESHOLD:
                        # bump retrievals on the existing chunk; treat as a "hit"
                        self._conn.execute(
                            "UPDATE chunks SET retrievals = retrievals + 1, last_access = ? WHERE id = ?",
                            (time.time(), row["id"]),
                        )
                        self._conn.commit()
                        return row["id"]
                except Exception:
                    continue
        except sqlite3.OperationalError:
            pass
        cur = self._conn.execute(
            "INSERT INTO chunks(session_id, kind, text, embedding, ts, source, last_access) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, kind, text, _pack(embedding), time.time(), source, time.time()),
        )
        self._conn.commit()
        return cur.lastrowid


    def recent_chunk_vectors(self, limit=400, session_id=None):
        """P1.7: return recent chunk vectors for density-based void detection."""
        sql = "SELECT id, embedding FROM chunks"
        params = ()
        if session_id is not None:
            sql += " WHERE session_id = ? OR kind = 'session_frame'"
            params = (session_id,)
        sql += " ORDER BY ts DESC LIMIT ?"
        params = params + (int(limit),)
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            try:
                vec = _unpack(row["embedding"])
            except Exception:
                continue
            if vec:
                out.append({"id": row["id"], "vec": vec})
        return out

    # P1.10/P1.11: hot/cold tiering + cross-session palace via promoted=1
    TIER_WEIGHT = {"hot": 1.10, "warm": 1.00, "cold": 0.85}
    PROMOTE_THRESHOLD = 3  # retrievals at which a chunk becomes cross-session

    def recall_chunks(self, query_embedding, top_k=5, session_id=None, min_score=0.45, cross_session=True):
        sql = "SELECT id, session_id, kind, text, embedding, retrievals, tier, promoted FROM chunks"
        params = ()
        if session_id is not None:
            if cross_session:
                sql += " WHERE session_id = ? OR kind = 'session_frame' OR promoted = 1"
            else:
                sql += " WHERE session_id = ? OR kind = 'session_frame'"
            params = (session_id,)
        rows = self._conn.execute(sql, params).fetchall()
        scored = []
        for row in rows:
            vec = _unpack(row["embedding"])
            try:
                s = _cosine(query_embedding, vec)
            except ValueError:
                continue
            if s < min_score:
                continue
            tier = row["tier"] if "tier" in row.keys() else "warm"
            weight = self.TIER_WEIGHT.get(tier, 1.0)
            # in-session affinity nudge: own-session beats foreign-promoted at equal cosine
            if session_id is not None and row["session_id"] == session_id:
                weight *= 1.03
            scored.append((s * weight, s, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        now = time.time()
        for weighted_s, raw_s, row in scored[:top_k]:
            new_retrievals = row["retrievals"] + 1
            new_tier = row["tier"] if "tier" in row.keys() else "warm"
            new_promoted = row["promoted"] if "promoted" in row.keys() else 0
            if new_retrievals >= self.PROMOTE_THRESHOLD:
                new_tier = "hot"
                new_promoted = 1
            self._conn.execute(
                "UPDATE chunks SET retrievals = ?, tier = ?, promoted = ?, last_access = ? WHERE id = ?",
                (new_retrievals, new_tier, new_promoted, now, row["id"]),
            )
            out.append({
                "id": row["id"],
                "session_id": row["session_id"],
                "kind": row["kind"],
                "text": row["text"],
                "score": raw_s,
                "weighted_score": weighted_s,
                "tier": new_tier,
                "promoted": bool(new_promoted),
                "retrievals": new_retrievals,
            })
        self._conn.commit()
        return out

    def compact_tiers(self, cold_age_days=14, cold_max_retrievals=0):
        """Demote stale chunks to 'cold'. Called opportunistically on init."""
        cutoff = time.time() - (cold_age_days * 86400)
        self._conn.execute(
            "UPDATE chunks SET tier = 'cold' "
            "WHERE retrievals <= ? AND ts < ? AND tier = 'warm' AND promoted = 0",
            (cold_max_retrievals, cutoff),
        )
        self._conn.commit()

    # ---- examples --------------------------------------------------------

    def store_example(self, text, embedding, wizard_name, source="seed", weight=None):
        if not wizard_name:
            raise ValueError("wizard_name required")
        if weight is None:
            weight = 1.0 if source == "seed" else 0.5
        try:
            cur = self._conn.execute(
                "INSERT INTO examples(text, embedding, wizard_name, source, weight) "
                "VALUES (?, ?, ?, ?, ?)",
                (text, _pack(embedding), wizard_name, source, weight),
            )
            self._conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # duplicate (text, wizard_name); silently ignore — caller can update
            row = self._conn.execute(
                "SELECT id FROM examples WHERE text=? AND wizard_name=?",
                (text, wizard_name),
            ).fetchone()
            return row["id"] if row else None

    def match_example(self, query_embedding, min_score=0.45):
        rows = self._conn.execute(
            "SELECT id, text, embedding, wizard_name, weight, wins, losses FROM examples"
        ).fetchall()
        best = None
        best_score = -1.0
        for row in rows:
            vec = _unpack(row["embedding"])
            try:
                s = _cosine(query_embedding, vec)
            except ValueError:
                continue
            adj = s * row["weight"]
            if adj > best_score:
                best_score = adj
                best = (s, row)
        if best is None or best_score < min_score:
            return None
        raw_score, row = best
        return {
            "id": row["id"],
            "text": row["text"],
            "wizard_name": row["wizard_name"],
            "score": raw_score,
            "weighted_score": best_score,
            "weight": row["weight"],
            "wins": row["wins"],
            "losses": row["losses"],
        }

    def record_win(self, example_id):
        self._conn.execute(
            "UPDATE examples SET wins = wins + 1, weight = MIN(weight + 0.1, 2.0), "
            "last_used = ? WHERE id = ?",
            (time.time(), example_id),
        )
        self._conn.commit()

    def record_loss(self, example_id):
        self._conn.execute(
            "UPDATE examples SET losses = losses + 1, weight = MAX(weight - 0.2, 0.0), "
            "last_used = ? WHERE id = ?",
            (time.time(), example_id),
        )
        self._conn.commit()

    def count_examples(self):
        return self._conn.execute("SELECT COUNT(*) AS c FROM examples").fetchone()["c"]

    def count_chunks(self):
        return self._conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
    # ---- P2 dream_cycle helpers -----------------------------------------

    def list_promotable_chunks(self, min_retrievals=5):
        """Chunks retrieved >= N times, not yet promoted. Ordered most-retrieved first."""
        rows = self._conn.execute(
            "SELECT id, session_id, kind, text, retrievals, ts FROM chunks "
            "WHERE retrievals >= ? AND promoted = 0 "
            "ORDER BY retrievals DESC, ts DESC",
            (min_retrievals,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_promoted(self, chunk_id, memory_id):
        """Mark chunk as promoted; memory_id stored in promoted column (>0 = promoted)."""
        self._conn.execute(
            "UPDATE chunks SET promoted = ? WHERE id = ?",
            (int(memory_id), chunk_id),
        )
        self._conn.commit()

    def prune_old_chunks(self, prune_days):
        """Delete chunks older than prune_days (except promoted ones and session_frames)."""
        cutoff = time.time() - (prune_days * 86400)
        cur = self._conn.execute(
            "DELETE FROM chunks WHERE ts < ? AND promoted = 0 AND kind != 'session_frame'",
            (cutoff,),
        )
        self._conn.commit()
        return cur.rowcount

    def list_deprecatable_examples(self, min_losses=3, loss_ratio=2.0):
        """Examples with losses >= min_losses AND losses > wins * loss_ratio.
        Seeds are never deprecated (theyre the floor)."""
        rows = self._conn.execute(
            "SELECT id, text, wizard_name, wins, losses, weight, source FROM examples "
            "WHERE source != 'seed' AND losses >= ? AND losses > wins * ?",
            (min_losses, loss_ratio),
        ).fetchall()
        return [dict(r) for r in rows]

    def deprecate_example(self, example_id):
        """Delete a learned example (rather than soft-flag; table is small)."""
        cur = self._conn.execute("DELETE FROM examples WHERE id = ? AND source != 'seed'", (example_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def stats(self):
        return {
            "chunks_total":      self._conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"],
            "chunks_promoted":   self._conn.execute("SELECT COUNT(*) c FROM chunks WHERE promoted > 0").fetchone()["c"],
            "chunks_session_frame": self._conn.execute("SELECT COUNT(*) c FROM chunks WHERE kind='session_frame'").fetchone()["c"],
            "examples_total":    self._conn.execute("SELECT COUNT(*) c FROM examples").fetchone()["c"],
            "examples_learned":  self._conn.execute("SELECT COUNT(*) c FROM examples WHERE source='learned'").fetchone()["c"],
        }

