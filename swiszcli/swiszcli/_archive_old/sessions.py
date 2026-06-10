"""Session log: every user/assistant message into sqlite, with FTS.

Why: Hermes has session search; the local Caden needs it too. Cheap,
local, deterministic. Backed by FTS5 if available, else LIKE fallback
that fails-loud-degraded (we surface the mode).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    ts          REAL NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_msg_session ON messages(session_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS messages_ai
AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class SessionLog:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(SCHEMA)
        self.fts = False
        try:
            self._conn.executescript(FTS_SCHEMA)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def log(self, session_id: str, role: str, content: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO messages (session_id, ts, role, content) VALUES (?, ?, ?, ?)",
            (session_id, time.time(), role, content),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        if self.fts:
            rows = self._conn.execute(
                "SELECT m.id, m.session_id, m.ts, m.role, m.content "
                "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                "WHERE messages_fts MATCH ? ORDER BY m.ts DESC LIMIT ?",
                (query, limit),
            ).fetchall()
        else:
            like = f"%{query}%"
            rows = self._conn.execute(
                "SELECT id, session_id, ts, role, content FROM messages "
                "WHERE content LIKE ? ORDER BY ts DESC LIMIT ?",
                (like, limit),
            ).fetchall()
        return [{"id": r[0], "session_id": r[1], "ts": r[2],
                 "role": r[3], "content": r[4]} for r in rows]

    def by_session(self, session_id: str, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, ts, role, content FROM messages "
            "WHERE session_id = ? ORDER BY ts ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [{"id": r[0], "ts": r[1], "role": r[2], "content": r[3]} for r in rows]


_DEFAULT: SessionLog | None = None


def set_default(log: SessionLog | None) -> None:
    global _DEFAULT
    _DEFAULT = log


def get_default() -> SessionLog | None:
    return _DEFAULT
