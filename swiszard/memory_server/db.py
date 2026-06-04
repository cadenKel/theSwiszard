"""
db.py — SQLite schema and helpers for the swiszard memory server.

Database location: ~/.hermes/swiszard/memory.db

Tables:
  memories        — facts with content + content_vec + provenance + lifecycle
  memory_triggers — situational trigger texts + embeddings per memory
  repo_files      — auto-indexer file summaries + embeddings
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / ".hermes" / "swiszard" / "memory.db"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            content           TEXT    NOT NULL,
            content_vec       BLOB    NOT NULL,
            kind              TEXT    NOT NULL DEFAULT 'fact',
            session_id        TEXT    NOT NULL,
            turn              INTEGER NOT NULL DEFAULT -1,
            timestamp         INTEGER NOT NULL,
            source            TEXT    NOT NULL DEFAULT 'llm_extracted',
            ttl_seconds       INTEGER,
            tags              TEXT    DEFAULT '[]',
            deprecated        INTEGER NOT NULL DEFAULT 0,
            deprecated_reason TEXT,
            superseded_by     INTEGER REFERENCES memories(id),
            lesson            TEXT
        );

        CREATE TABLE IF NOT EXISTS memory_triggers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id    INTEGER NOT NULL
                             REFERENCES memories(id) ON DELETE CASCADE,
            trigger_text TEXT    NOT NULL,
            trigger_vec  BLOB    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS repo_files (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id  TEXT    NOT NULL,
            path     TEXT    NOT NULL UNIQUE,
            mtime    INTEGER NOT NULL,
            summary  TEXT    NOT NULL,
            vec      BLOB    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_memories_session
            ON memories(session_id);
        CREATE INDEX IF NOT EXISTS idx_memories_kind
            ON memories(kind);
        CREATE INDEX IF NOT EXISTS idx_memories_deprecated
            ON memories(deprecated);
        CREATE INDEX IF NOT EXISTS idx_triggers_memory
            ON memory_triggers(memory_id);
        CREATE INDEX IF NOT EXISTS idx_repo_files_repo
            ON repo_files(repo_id);
    """)
    # Idempotent migration for existing v1 DBs
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    if "deprecated" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0")
    if "deprecated_reason" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN deprecated_reason TEXT")
    if "superseded_by" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN superseded_by INTEGER REFERENCES memories(id)")
    if "lesson" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN lesson TEXT")
    conn.commit()


# ── memories ──────────────────────────────────────────────────────────────────

def insert_memory(
    conn: sqlite3.Connection,
    content: str,
    content_vec: bytes,
    kind: str,
    session_id: str,
    turn: int,
    source: str,
    ttl_seconds: int | None,
    tags: list[str],
) -> int:
    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO memories
           (content, content_vec, kind, session_id, turn, timestamp, source,
            ttl_seconds, tags)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (content, content_vec, kind, session_id, turn, now, source,
         ttl_seconds, json.dumps(tags)),
    )
    conn.commit()
    return cur.lastrowid


def insert_trigger(
    conn: sqlite3.Connection,
    memory_id: int,
    trigger_text: str,
    trigger_vec: bytes,
) -> None:
    conn.execute(
        "INSERT INTO memory_triggers (memory_id, trigger_text, trigger_vec) "
        "VALUES (?, ?, ?)",
        (memory_id, trigger_text, trigger_vec),
    )
    conn.commit()


def delete_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    return cur.rowcount > 0


def deprecate_memory(
    conn: sqlite3.Connection,
    memory_id: int,
    reason: str | None = None,
) -> bool:
    cur = conn.execute(
        "UPDATE memories SET deprecated = 1, deprecated_reason = ? WHERE id = ?",
        (reason, memory_id),
    )
    conn.commit()
    return cur.rowcount > 0


def supersede_memory(
    conn: sqlite3.Connection,
    old_memory_id: int,
    new_memory_id: int,
    lesson: str | None = None,
) -> bool:
    cur = conn.execute(
        """UPDATE memories
           SET deprecated = 1,
               superseded_by = ?,
               lesson = COALESCE(?, lesson),
               deprecated_reason = COALESCE(deprecated_reason, 'superseded')
           WHERE id = ?""",
        (new_memory_id, lesson, old_memory_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_memory_row(conn: sqlite3.Connection, memory_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()


def update_tags(conn: sqlite3.Connection, memory_id: int, tags: list[str]) -> bool:
    cur = conn.execute(
        "UPDATE memories SET tags = ? WHERE id = ?",
        (json.dumps(tags), memory_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_pinned_memory_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all active memories tagged 'always_inject'."""
    now = int(time.time())
    return conn.execute(
        """SELECT id, content, kind, session_id, turn, timestamp, tags
           FROM memories
           WHERE deprecated = 0
             AND tags LIKE '%always_inject%'
             AND (ttl_seconds IS NULL OR (timestamp + ttl_seconds) > ?)
           ORDER BY id""",
        (now,),
    ).fetchall()


def get_active_trigger_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return non-expired, non-deprecated trigger rows joined with memory."""
    now = int(time.time())
    return conn.execute(
        """SELECT mt.memory_id, mt.trigger_text, mt.trigger_vec,
                  m.content, m.kind, m.session_id, m.turn, m.timestamp, m.tags
           FROM memory_triggers mt
           JOIN memories m ON m.id = mt.memory_id
           WHERE m.deprecated = 0
             AND (m.ttl_seconds IS NULL OR (m.timestamp + m.ttl_seconds) > ?)""",
        (now,),
    ).fetchall()


# Kept for backward compat (used by /recall_content, includes deprecated)
def get_all_trigger_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    now = int(time.time())
    return conn.execute(
        """SELECT mt.memory_id, mt.trigger_text, mt.trigger_vec,
                  m.content, m.kind, m.session_id, m.turn, m.timestamp, m.tags,
                  m.deprecated, m.superseded_by, m.lesson
           FROM memory_triggers mt
           JOIN memories m ON m.id = mt.memory_id
           WHERE m.ttl_seconds IS NULL
              OR (m.timestamp + m.ttl_seconds) > ?""",
        (now,),
    ).fetchall()


def get_all_memory_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All non-expired memories (deprecated INCLUDED — for forensic /recall_content)."""
    now = int(time.time())
    return conn.execute(
        """SELECT id, content, content_vec, kind, session_id, turn, timestamp,
                  tags, deprecated, deprecated_reason, superseded_by, lesson
           FROM memories
           WHERE ttl_seconds IS NULL
              OR (timestamp + ttl_seconds) > ?""",
        (now,),
    ).fetchall()


# ── repo_files ────────────────────────────────────────────────────────────────

def upsert_repo_file(
    conn: sqlite3.Connection,
    repo_id: str,
    path: str,
    mtime: int,
    summary: str,
    vec: bytes,
) -> None:
    conn.execute(
        """INSERT INTO repo_files (repo_id, path, mtime, summary, vec)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
               mtime   = excluded.mtime,
               summary = excluded.summary,
               vec     = excluded.vec""",
        (repo_id, path, mtime, summary, vec),
    )
    conn.commit()


def get_repo_file_rows(conn: sqlite3.Connection, repo_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT path, mtime, summary, vec FROM repo_files WHERE repo_id = ?",
        (repo_id,),
    ).fetchall()


def count_rows(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "memories":           conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
        "memories_active":    conn.execute("SELECT COUNT(*) FROM memories WHERE deprecated=0").fetchone()[0],
        "memories_deprecated":conn.execute("SELECT COUNT(*) FROM memories WHERE deprecated=1").fetchone()[0],
        "memory_triggers":    conn.execute("SELECT COUNT(*) FROM memory_triggers").fetchone()[0],
        "repo_files":         conn.execute("SELECT COUNT(*) FROM repo_files").fetchone()[0],
    }
