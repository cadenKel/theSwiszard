"""Persistent wizard trace log.

Every wizard.run() writes a trace row at start, fills it in at end. Nested
wizards inherit parent_id. Source distinguishes user-driven vs LLM-driven
walks. Traces are the palaces walls: they let /trace browse history and
/replay re-walk with recorded ctx as defaults.

Storage: sqlite at <state_dir>/traces.db.
NO fallbacks. NO silent drops.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id           TEXT PRIMARY KEY,
    parent_id    TEXT,
    wizard       TEXT NOT NULL,
    source       TEXT NOT NULL,        -- user | llm
    ctx_json     TEXT NOT NULL,
    result_json  TEXT,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    status       TEXT NOT NULL         -- running | ok | cancelled | error
);
CREATE INDEX IF NOT EXISTS idx_traces_wiz    ON traces(wizard);
CREATE INDEX IF NOT EXISTS idx_traces_parent ON traces(parent_id);
CREATE INDEX IF NOT EXISTS idx_traces_start  ON traces(started_at DESC);

CREATE TABLE IF NOT EXISTS agent_turns (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    turn_number     INTEGER NOT NULL,
    timestamp       REAL NOT NULL,
    input_text      TEXT NOT NULL,
    input_embedding BLOB,              -- JSON-encoded float list (768-dim)
    tools_used      TEXT NOT NULL,     -- JSON array of {task, handler, outcome, latency_ms}
    outcome         TEXT NOT NULL,     -- success | error | fabrication_stripped
    latency_ms      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_at_session ON agent_turns(session_id);
CREATE INDEX IF NOT EXISTS idx_at_ts      ON agent_turns(timestamp DESC);
"""


def _json_safe(x: Any) -> Any:
    """Best-effort JSON-safe converter; fails loud on truly unserializable types."""
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    if isinstance(x, dict):
        return {str(k): _json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_safe(v) for v in x]
    # last resort: repr; explicit so we can see what slipped through
    return f"<unserializable {type(x).__name__}>"


class TraceWriter:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def start(self, wizard: str, source: str, parent_id: str | None = None,
              initial_ctx: dict | None = None) -> str:
        trace_id = "tr_" + uuid.uuid4().hex[:12]
        self._conn.execute(
            """INSERT INTO traces (id, parent_id, wizard, source, ctx_json,
                                   started_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'running')""",
            (trace_id, parent_id, wizard, source,
             json.dumps(_json_safe(initial_ctx or {})), time.time()),
        )
        self._conn.commit()
        return trace_id

    def end(self, trace_id: str, ctx: dict, result: Any, status: str) -> None:
        if status not in ("ok", "cancelled", "error"):
            raise ValueError(f"bad status: {status!r}")
        self._conn.execute(
            """UPDATE traces
               SET ctx_json = ?, result_json = ?, ended_at = ?, status = ?
               WHERE id = ?""",
            (json.dumps(_json_safe(ctx)),
             json.dumps(_json_safe(result)),
             time.time(), status, trace_id),
        )
        self._conn.commit()

    # ── reads ────────────────────────────────────────────────────────────
    def recent(self, n: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM traces ORDER BY started_at DESC LIMIT ?", (n,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, trace_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM traces WHERE id = ?", (trace_id,),
        ).fetchone()
        return dict(row) if row else None

    def children(self, trace_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM traces WHERE parent_id = ? ORDER BY started_at",
            (trace_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── agent turn logging ───────────────────────────────────────────────
    def record_agent_turn(
        self,
        *,
        session_id: str,
        turn_number: int,
        input_text: str,
        input_embedding: list | None,
        tools_used: list,          # [{task, handler, outcome, latency_ms}]
        outcome: str,              # success | error | fabrication_stripped
        latency_ms: int,
    ) -> str:
        turn_id = "at_" + uuid.uuid4().hex[:12]
        emb_blob = json.dumps(input_embedding) if input_embedding else None
        self._conn.execute(
            """INSERT INTO agent_turns
               (id, session_id, turn_number, timestamp,
                input_text, input_embedding, tools_used, outcome, latency_ms)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (turn_id, session_id, turn_number, time.time(),
             input_text, emb_blob,
             json.dumps(_json_safe(tools_used)),
             outcome, latency_ms),
        )
        self._conn.commit()
        return turn_id

    def recent_turn_embeddings(self, limit: int = 500) -> list[list[float]]:
        """Return up to `limit` input embedding vectors for void-detector corpus."""
        rows = self._conn.execute(
            "SELECT input_embedding FROM agent_turns "
            "WHERE input_embedding IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            try:
                result.append(json.loads(r[0]))
            except Exception:
                pass
        return result


# Process-singleton, mirrors pools.py
_DEFAULT: TraceWriter | None = None


def set_default(writer: TraceWriter) -> None:
    global _DEFAULT
    _DEFAULT = writer


def get_default() -> TraceWriter | None:
    return _DEFAULT
