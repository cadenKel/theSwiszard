"""
store.py — swiszcontext: overlapping conversation frame store.

Schema:
  context_frame  — id, session_id, turn_number, role, content, vec, created
  frame_overlap  — id, frame_a_id, frame_b_id, score (precomputed pairwise similarity)

Retrieval: pure cosine similarity on frame vectors. No triggers. No model calls.
Presentation: chronological transcript collage, oldest first.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / ".swiszard" / "swiszcontext.db"


def _db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS context_frame (
            id          INTEGER PRIMARY KEY,
            session_id  TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            vec         BLOB,
            created     INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ctx_session ON context_frame(session_id);
        CREATE INDEX IF NOT EXISTS idx_ctx_created ON context_frame(created);

        CREATE TABLE IF NOT EXISTS frame_overlap (
            id          INTEGER PRIMARY KEY,
            frame_a_id  INTEGER NOT NULL REFERENCES context_frame(id),
            frame_b_id  INTEGER NOT NULL REFERENCES context_frame(id),
            score       REAL NOT NULL,
            created     INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_overlap_a ON frame_overlap(frame_a_id);
        CREATE INDEX IF NOT EXISTS idx_overlap_b ON frame_overlap(frame_b_id);
    """)


def _embed_to_blob(text: str) -> bytes:
    """Embed text to 768-dim nomic-embed-text blob via Ollama HTTP."""
    import urllib.request
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/embeddings",
        data=json.dumps({"model": "nomic-embed-text", "prompt": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    vec = data["embedding"]
    import struct
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_array(blob: bytes) -> list[float]:
    import struct
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


class ContextStore:
    def __init__(self, db_path: str | Path | None = None):
        if db_path:
            global DB_PATH
            DB_PATH = Path(db_path)
        self._conn = _db_connect()
        _init_db(self._conn)

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __del__(self):
        self.close()

    def append_turn(self, session_id: str, turn_number: int, role: str, content: str) -> int:
        """Store a conversation turn. Returns frame_id."""
        vec = _embed_to_blob(content)
        cur = self._conn.execute(
            "INSERT INTO context_frame (session_id, turn_number, role, content, vec) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, turn_number, role, content, vec),
        )
        frame_id = cur.lastrowid
        self._conn.commit()
        # Compute overlaps with recent frames from other sessions
        self._compute_overlaps(frame_id, vec)
        return frame_id

    def _compute_overlaps(self, frame_id: int, vec: bytes, lookback: int = 200):
        """Precompute similarity with recent frames from other sessions."""
        arr = _blob_to_array(vec)
        rows = self._conn.execute(
            "SELECT id, vec FROM context_frame WHERE id != ? ORDER BY created DESC LIMIT ?",
            (frame_id, lookback),
        ).fetchall()
        for row in rows:
            if row["vec"] is None:
                continue
            other_arr = _blob_to_array(bytes(row["vec"]))
            score = _cosine_similarity(arr, other_arr)
            if score > 0.5:  # only store meaningful overlaps
                self._conn.execute(
                    "INSERT INTO frame_overlap (frame_a_id, frame_b_id, score) VALUES (?, ?, ?)",
                    (frame_id, row["id"], score),
                )
        self._conn.commit()

    def recall(self, situation_text: str, top_k: int = 5, session_id: str | None = None) -> list[dict]:
        """Retrieve frames by pure similarity to situation_text. Returns chronological transcript collage."""
        qvec = _embed_to_blob(situation_text)
        qarr = _blob_to_array(qvec)

        query = "SELECT id, session_id, turn_number, role, content, vec, created FROM context_frame"
        params: list[Any] = []
        if session_id:
            query += " WHERE session_id = ?"
            params.append(session_id)
        query += " ORDER BY created DESC LIMIT 500"  # scan recent 500

        rows = self._conn.execute(query, params).fetchall()
        scored = []
        for row in rows:
            if row["vec"] is None:
                continue
            arr = _blob_to_array(bytes(row["vec"]))
            score = _cosine_similarity(qarr, arr)
            scored.append((score, dict(row)))

        scored.sort(key=lambda x: -x[0])
        top = scored[:top_k]

        # Return chronological (oldest first) for transcript collage
        top.sort(key=lambda x: (x[1]["session_id"], x[1]["turn_number"]))

        return [
            {
                "frame_id": r["id"],
                "session_id": r["session_id"],
                "turn_number": r["turn_number"],
                "role": r["role"],
                "content": r["content"],
                "score": round(score, 4),
                "created": r["created"],
            }
            for score, r in top
        ]

    def get_session_frames(self, session_id: str) -> list[dict]:
        """Get all frames for a session, chronological."""
        rows = self._conn.execute(
            "SELECT id, turn_number, role, content, created FROM context_frame "
            "WHERE session_id = ? ORDER BY turn_number",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, session_id: str | None = None) -> dict:
        if session_id:
            row = self._conn.execute(
                "SELECT COUNT(*) as n FROM context_frame WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return {"session_id": session_id, "frame_count": row["n"]}
        row = self._conn.execute("SELECT COUNT(*) as n FROM context_frame").fetchone()
        sessions = self._conn.execute(
            "SELECT session_id, COUNT(*) as n FROM context_frame GROUP BY session_id"
        ).fetchall()
        return {
            "total_frames": row["n"],
            "sessions": {r["session_id"]: r["n"] for r in sessions},
        }
