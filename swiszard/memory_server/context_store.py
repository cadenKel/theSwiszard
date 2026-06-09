"""
context_store.py — swiszcontext: overlapping conversation frame store.

Conversation history is sliced into overlapping windows of up to WINDOW_SIZE
messages, each frame embedded and stored. At retrieval time frames compete on:

    score = cosine_sim(frame_vec, situation_vec) * recency_bias(age_in_turns)

Last PINNED_TURNS frames are always returned regardless of score.
Overlapping frames sharing messages compete — only the highest scorer per
message set survives (soft dedup via max marginal relevance).

DB: ~/.hermes/swiszard/context.db  (separate from memory.db)
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path

from .embed import embed, blob_to_array, cosine_similarity

DB_PATH      = Path.home() / ".hermes" / "swiszard" / "context.db"
WINDOW_SIZE  = 8    # max messages per frame
STRIDE       = 4    # overlap stride — frames share WINDOW_SIZE - STRIDE msgs
PINNED_TURNS = 3    # last N turns always injected regardless of score
DECAY_LAMBDA = 2.5  # exponential decay rate for recency bias


SCHEMA = """
CREATE TABLE IF NOT EXISTS context_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS context_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    turn_number INTEGER NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  INTEGER NOT NULL,
    UNIQUE(session_id, turn_number, role)
);
CREATE TABLE IF NOT EXISTS context_frames (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    frame_index INTEGER NOT NULL,
    turn_start  INTEGER NOT NULL,
    turn_end    INTEGER NOT NULL,
    messages    TEXT    NOT NULL,
    vector      BLOB    NOT NULL,
    created_at  INTEGER NOT NULL,
    UNIQUE(session_id, frame_index)
);
CREATE INDEX IF NOT EXISTS idx_cf_session  ON context_frames(session_id);
CREATE INDEX IF NOT EXISTS idx_cm_session  ON context_messages(session_id);
"""


def _get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _render_frame(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        parts.append(f"[{m.get('role','?')}] {m.get('content','')}")
    return "\n".join(parts)


def _recency_bias(age_in_turns: int) -> float:
    """Exponential decay. age=0 → 1.0; decays toward 0 as age grows."""
    return math.exp(-DECAY_LAMBDA * age_in_turns / max(1, WINDOW_SIZE))


class ContextStore:
    """Per-session overlapping conversation frame store (swiszcontext)."""

    def __init__(self, db_path: Path = DB_PATH):
        self._conn = _get_conn(db_path)

    def close(self) -> None:
        self._conn.close()

    # ── write ─────────────────────────────────────────────────────────────────

    def append_turn(self, session_id: str, turn_number: int,
                    role: str, content: str) -> None:
        """Append one message and rebuild affected frames."""
        self._conn.execute(
            "INSERT OR IGNORE INTO context_sessions (session_id, created_at)"
            " VALUES (?, ?)", (session_id, int(time.time())),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO context_messages"
            " (session_id, turn_number, role, content, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, turn_number, role, content, int(time.time())),
        )
        self._conn.commit()
        self._rebuild_frames(session_id)

    def _rebuild_frames(self, session_id: str) -> None:
        rows = self._conn.execute(
            "SELECT turn_number, role, content FROM context_messages"
            " WHERE session_id = ? ORDER BY turn_number, role",
            (session_id,),
        ).fetchall()
        messages = [
            {"role": r["role"], "content": r["content"], "turn": r["turn_number"]}
            for r in rows
        ]
        if not messages:
            return

        windows = []
        i = 0
        while i < len(messages):
            windows.append(messages[i: i + WINDOW_SIZE])
            i += STRIDE
            if i >= len(messages):
                break

        for frame_idx, window in enumerate(windows):
            text  = _render_frame(window)
            vec   = embed(text).tobytes()
            t_start = window[0]["turn"]
            t_end   = window[-1]["turn"]
            self._conn.execute(
                """INSERT OR REPLACE INTO context_frames
                   (session_id, frame_index, turn_start, turn_end,
                    messages, vector, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, frame_idx, t_start, t_end,
                 json.dumps(window), vec, int(time.time())),
            )
        self._conn.commit()

    # ── read ──────────────────────────────────────────────────────────────────

    def recall(self, session_id: str, situation_text: str,
               top_k: int = 5) -> list[dict]:
        """
        Return top_k most relevant frames for situation_text.
        Last PINNED_TURNS turns always included.
        Overlapping frames compete — higher scorer wins when turns overlap.
        """
        rows = self._conn.execute(
            "SELECT * FROM context_frames WHERE session_id = ?"
            " ORDER BY frame_index DESC",
            (session_id,),
        ).fetchall()
        if not rows:
            return []

        sit_vec  = embed(situation_text)
        max_turn = max(r["turn_end"] for r in rows)
        scored: list[tuple[float, object]] = []

        for row in rows:
            fvec = blob_to_array(bytes(row["vector"]))
            cos  = cosine_similarity(sit_vec, fvec)
            age  = max(0, max_turn - row["turn_end"])
            score = (1e9 + cos) if age < PINNED_TURNS else (cos * _recency_bias(age))
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)

        covered: set[tuple] = set()
        results: list[dict] = []

        for score, row in scored:
            msgs = json.loads(row["messages"])
            msg_key = frozenset((m["turn"], m["role"]) for m in msgs)
            if msg_key <= covered:
                continue
            covered |= msg_key
            pinned  = score > 1e8
            results.append({
                "frame_index": row["frame_index"],
                "turn_start":  row["turn_start"],
                "turn_end":    row["turn_end"],
                "score":       round(cos, 4),
                "pinned":      pinned,
                "messages":    msgs,
                "text":        _render_frame(msgs),
            })
            if len(results) >= top_k + PINNED_TURNS:
                break

        return results

    def stats(self, session_id: str) -> dict:
        frames = self._conn.execute(
            "SELECT COUNT(*) c FROM context_frames WHERE session_id=?",
            (session_id,),
        ).fetchone()["c"]
        msgs = self._conn.execute(
            "SELECT COUNT(*) c FROM context_messages WHERE session_id=?",
            (session_id,),
        ).fetchone()["c"]
        return {"session_id": session_id, "frames": frames, "messages": msgs}
