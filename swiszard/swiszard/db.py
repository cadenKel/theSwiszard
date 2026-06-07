"""
db.py — SQLite schema and query helpers for swiszard's example routing table.

Database location: ~/.hermes/swiszard/routes.db

Schema:
  examples(id INTEGER PK, phrasing TEXT, handler TEXT, embedding BLOB,
           success_count INT DEFAULT 0, fail_count INT DEFAULT 0,
           created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.path.expanduser("~/.hermes/swiszard/routes.db"))


def get_connection() -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database and return a connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the examples table if it doesn't already exist."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS examples (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                phrasing     TEXT NOT NULL,
                handler      TEXT NOT NULL,
                embedding    BLOB NOT NULL,
                success_count INTEGER DEFAULT 0,
                fail_count    INTEGER DEFAULT 0,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gaps (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                phrasing     TEXT NOT NULL,
                best_guess   TEXT NOT NULL,
                sim          REAL NOT NULL,
                resolved     INTEGER DEFAULT 0,
                resolved_handler TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def record_gap(phrasing: str, best_guess: str, sim: float) -> int:
    """Log a no-match gap for later resolution. Returns new row id."""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO gaps (phrasing, best_guess, sim) VALUES (?,?,?)",
            (phrasing, best_guess, round(sim, 4)),
        )
        conn.commit()
        return cur.lastrowid


def resolve_gap(gap_id: int, handler: str) -> None:
    """Mark a gap resolved once the LLM or user confirms the correct handler."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE gaps SET resolved=1, resolved_handler=? WHERE id=?",
            (handler, gap_id),
        )
        conn.commit()


def get_all_examples(conn: sqlite3.Connection) -> list:
    """Return all rows from the examples table."""
    return conn.execute(
        "SELECT id, phrasing, handler, embedding, success_count, fail_count "
        "FROM examples"
    ).fetchall()


def insert_example(
    conn: sqlite3.Connection, phrasing: str, handler: str, embedding_blob: bytes
) -> int:
    """Insert a new example and return its rowid."""
    cur = conn.execute(
        "INSERT INTO examples (phrasing, handler, embedding, success_count, fail_count) "
        "VALUES (?, ?, ?, 0, 0)",
        (phrasing, handler, embedding_blob),
    )
    conn.commit()
    return cur.lastrowid


def increment_success(conn: sqlite3.Connection, example_id: int) -> None:
    conn.execute(
        "UPDATE examples SET success_count = success_count + 1 WHERE id = ?",
        (example_id,),
    )
    conn.commit()


def increment_fail(conn: sqlite3.Connection, example_id: int) -> None:
    conn.execute(
        "UPDATE examples SET fail_count = fail_count + 1 WHERE id = ?",
        (example_id,),
    )
    conn.commit()


def count_examples() -> int:
    """Return the total number of rows in the examples table."""
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM examples").fetchone()[0]
