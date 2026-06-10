"""
session_logger.py — per-session JSONL tool log + sqlite chat log.

Written to match the exact format swisz_log_cli.py already reads:
  ~/.swiszcli/swisz_calls/swisz_<8hex>.jsonl  — one record per tool call
  ~/.swiszcli/sessions.db                      — messages table (chat turns)
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path


_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    session_id  TEXT NOT NULL,
    ts          REAL NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id);
"""


class SessionLogger:
    def __init__(self, state_dir: Path, session_id: str) -> None:
        self.session_id = session_id
        self.state_dir  = state_dir

        # ── JSONL tool log ────────────────────────────────────────────────────
        calls_dir = state_dir / "swisz_calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = calls_dir / f"{session_id}.jsonl"
        self._jsonl_fh   = open(self._jsonl_path, "a", encoding="utf-8", buffering=1)

        # symlink latest → current file
        latest = calls_dir / "latest"
        if latest.is_symlink() or latest.exists():
            latest.unlink(missing_ok=True)
        latest.symlink_to(self._jsonl_path)

        # ── sqlite chat log ───────────────────────────────────────────────────
        db_path = state_dir / "sessions.db"
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SESSIONS_SCHEMA)
        self._conn.commit()

    # ── public API ────────────────────────────────────────────────────────────

    def log_message(self, role: str, content: str) -> None:
        """Record a chat turn (user or assistant)."""
        self._conn.execute(
            "INSERT INTO messages (session_id, ts, role, content) VALUES (?,?,?,?)",
            (self.session_id, time.time(), role, content),
        )
        self._conn.commit()

    def log_tool_call(
        self,
        *,
        handler:     str,
        task:        str,
        result:      str,
        duration_ms: int,
        error:       str | None = None,
    ) -> None:
        """Record one MCP tool call."""
        record = {
            "call_id":     uuid.uuid4().hex[:12],
            "ts":          time.time(),
            "session_id":  self.session_id,
            "handler":     handler,
            "task":        task,
            "result":      result,
            "duration_ms": duration_ms,
            "error":       error,
        }
        self._jsonl_fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def close(self) -> None:
        try:
            self._jsonl_fh.close()
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
