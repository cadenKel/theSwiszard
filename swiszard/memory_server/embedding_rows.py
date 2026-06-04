"""
embedding_rows.py — multi-vector retrieval surface for swizmem (phase 1).

Each memory contributes 1+ rows here, one per (kind, source). Recall scans
this table, groups by memory_id, keeps the max weighted similarity. This
lets trigger phrases be first-class retrieval surfaces without diluting
into raw content embeddings.

Table is additive — legacy memories.content_vec and memory_triggers.trigger_vec
remain populated for backward compat / rollback. This module is the new
read path; writes are dual to legacy + here.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable

# kind weights for max-pool retrieval. trigger phrases win ties over raw
# (the entire point of the rewrite). Future kinds (intent, summary) can be
# added without schema changes.
KIND_WEIGHTS: dict[str, float] = {
	"raw":     0.85,
	"trigger": 1.00,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_rows (
	id          INTEGER PRIMARY KEY AUTOINCREMENT,
	memory_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
	kind        TEXT    NOT NULL,
	source_id   INTEGER,
	source_text TEXT    NOT NULL,
	vector      BLOB    NOT NULL,
	created_at  INTEGER NOT NULL,
	UNIQUE(memory_id, kind, source_id)
);
CREATE INDEX IF NOT EXISTS idx_embedding_rows_mem  ON embedding_rows(memory_id);
CREATE INDEX IF NOT EXISTS idx_embedding_rows_kind ON embedding_rows(kind);
"""


def init_schema(conn: sqlite3.Connection) -> None:
	conn.executescript(SCHEMA)
	conn.commit()


def insert_row(
	conn: sqlite3.Connection,
	memory_id: int,
	kind: str,
	source_id: int | None,
	source_text: str,
	vector: bytes,
) -> None:
	if kind not in KIND_WEIGHTS:
		raise ValueError(f"unknown embedding kind {kind!r}; add to KIND_WEIGHTS first")
	conn.execute(
		"""INSERT OR REPLACE INTO embedding_rows
		   (memory_id, kind, source_id, source_text, vector, created_at)
		   VALUES (?, ?, ?, ?, ?, ?)""",
		(memory_id, kind, source_id, source_text, vector, int(time.time())),
	)
	conn.commit()


def delete_rows_for_memory(conn: sqlite3.Connection, memory_id: int) -> None:
	conn.execute("DELETE FROM embedding_rows WHERE memory_id = ?", (memory_id,))
	conn.commit()


def get_active_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	"""Rows joined with memory; excludes deprecated and TTL-expired."""
	now = int(time.time())
	return conn.execute(
		"""SELECT er.memory_id, er.kind, er.source_text, er.vector,
		          m.content, m.kind AS mem_kind, m.session_id, m.turn,
		          m.timestamp, m.tags
		   FROM embedding_rows er
		   JOIN memories m ON m.id = er.memory_id
		   WHERE m.deprecated = 0
		     AND (m.ttl_seconds IS NULL OR (m.timestamp + m.ttl_seconds) > ?)""",
		(now,),
	).fetchall()


def needs_backfill(conn: sqlite3.Connection) -> bool:
	"""True iff embedding_rows is empty but memories has rows."""
	mem_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
	if mem_count == 0:
		return False
	er_count = conn.execute("SELECT COUNT(*) FROM embedding_rows").fetchone()[0]
	return er_count == 0


def backfill(conn: sqlite3.Connection) -> dict:
	"""One-shot backfill from legacy columns. Idempotent: skips rows that
	already have an entry. Returns counts for logging."""
	now = int(time.time())
	raw_added = 0
	trig_added = 0

	# raw rows from memories.content_vec
	for r in conn.execute(
		"SELECT id, content, content_vec FROM memories WHERE content_vec IS NOT NULL"
	).fetchall():
		exists = conn.execute(
			"SELECT 1 FROM embedding_rows WHERE memory_id=? AND kind='raw' AND source_id IS NULL",
			(r["id"],),
		).fetchone()
		if exists:
			continue
		conn.execute(
			"""INSERT OR IGNORE INTO embedding_rows
			   (memory_id, kind, source_id, source_text, vector, created_at)
			   VALUES (?, 'raw', NULL, ?, ?, ?)""",
			(r["id"], r["content"], bytes(r["content_vec"]), now),
		)
		raw_added += 1

	# trigger rows from memory_triggers
	for t in conn.execute(
		"SELECT id, memory_id, trigger_text, trigger_vec FROM memory_triggers"
	).fetchall():
		exists = conn.execute(
			"SELECT 1 FROM embedding_rows WHERE memory_id=? AND kind='trigger' AND source_id=?",
			(t["memory_id"], t["id"]),
		).fetchone()
		if exists:
			continue
		conn.execute(
			"""INSERT OR IGNORE INTO embedding_rows
			   (memory_id, kind, source_id, source_text, vector, created_at)
			   VALUES (?, 'trigger', ?, ?, ?, ?)""",
			(t["memory_id"], t["id"], t["trigger_text"], bytes(t["trigger_vec"]), now),
		)
		trig_added += 1

	conn.commit()
	return {"raw_added": raw_added, "trigger_added": trig_added}
