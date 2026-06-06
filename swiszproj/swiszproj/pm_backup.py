"""
pm_backup.py - Append-only JSONL backup for project manager mutations.

Logged to ~/.hermes/swiszard/pm_backups/<date>.jsonl
Write-before-mutation: log line is written BEFORE the DB mutation.
Append-only, never truncate, rotate daily by filename.
Never catch exceptions in log_mutation - fail loud, never silent data loss.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

BACKUP_DIR = Path.home() / ".hermes" / "swiszard" / "pm_backups"


def _today_path():
    """Return today's backup file, creating directory if needed."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return BACKUP_DIR / f"{today}.jsonl"


def log_mutation(op, table, row_id, old_row=None, new_row=None, metadata=None):
    """Append a mutation record to today's JSONL backup.
    
    Args:
        op: "INSERT" | "UPDATE" | "DELETE"
        table: "pm_node" | "pm_project" 
        row_id: integer primary key of the affected row
        old_row: dict of pre-mutation state (required for UPDATE/DELETE)
        new_row: dict of post-mutation state (required for INSERT/UPDATE)
        metadata: optional dict with caller info (session_id, reason, etc.)
    
    Returns the path written to.
    """
    record = {
        "ts": time.time(),
        "op": op,
        "table": table,
        "row_id": row_id,
        "old_row": old_row,
        "new_row": new_row,
        "metadata": metadata or {},
    }
    path = _today_path()
    with open(path, "a") as f:
        f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + chr(10))
    return str(path)


def snapshot_project(conn, project_id: int) -> str:
    """Full snapshot of all nodes in a project. Use before dangerous operations."""
    import sqlite3
    conn.row_factory = sqlite3.Row
    nodes = conn.execute(
        "SELECT id, project_id, parent_id, kind, state, title, body, created, updated, tags FROM pm_node WHERE project_id=? ORDER BY id", (project_id,)
    ).fetchall()
    project = conn.execute(
        "SELECT * FROM pm_project WHERE id=?", (project_id,)
    ).fetchone()
    
    snapshot = {
        "ts": time.time(),
        "op": "SNAPSHOT",
        "project": dict(project) if project else None,
        "node_count": len(nodes),
        "nodes": [dict(n) for n in nodes],
    }
    path = _today_path()
    with open(path, "a") as f:
        f.write(json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False) + chr(10))
    return str(path)
