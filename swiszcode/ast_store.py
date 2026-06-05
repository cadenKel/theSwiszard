"""
ast_store.py — persistent AST index for swiszcode.

Stores extracted ASTs per-file in a SQLite database so the visualizer
and transform module can query without re-parsing. Not swiszmem —
swiszmem owns embeddings; swiszcode owns structured AST data.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ast_extract import extract_ast

DEFAULT_DB = Path.home() / ".swiszcode" / "ast_store.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ast_files (
    filepath TEXT PRIMARY KEY,
    file_hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    indexed_at INTEGER NOT NULL,
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ast_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT NOT NULL REFERENCES ast_files(filepath) ON DELETE CASCADE,
    node_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    depth INTEGER NOT NULL,
    lineno INTEGER NOT NULL,
    end_lineno INTEGER NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    UNIQUE(filepath, node_id)
);
CREATE TABLE IF NOT EXISTS ast_edges (
    filepath TEXT NOT NULL REFERENCES ast_files(filepath) ON DELETE CASCADE,
    from_id INTEGER NOT NULL,
    to_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ast_nodes_filepath ON ast_nodes(filepath);
CREATE INDEX IF NOT EXISTS idx_ast_nodes_type ON ast_nodes(type);
CREATE INDEX IF NOT EXISTS idx_ast_edges_filepath ON ast_edges(filepath);
"""


def _file_hash(filepath: str | Path) -> str:
    """Fast content hash — just mtime+size, good enough for change detection."""
    p = Path(filepath)
    stat = p.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


class ASTStore:
    """Persistent store for extracted ASTs."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or DEFAULT_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def index_file(self, filepath: str | Path) -> dict:
        """Extract and store AST for a single file. Returns summary dict."""
        fp = str(Path(filepath).resolve())
        fhash = _file_hash(fp)

        # Check if already indexed and fresh
        row = self.conn.execute(
            "SELECT file_hash FROM ast_files WHERE filepath=?",
            (fp,),
        ).fetchone()
        if row and row[0] == fhash:
            # Fresh — return summary without re-extracting
            row = self.conn.execute(
                "SELECT node_count, edge_count, indexed_at FROM ast_files WHERE filepath=?",
                (fp,),
            ).fetchone()
            return {
                "file": fp,
                "fresh": True,
                "node_count": row[0],
                "edge_count": row[1],
                "indexed_at": row[2],
            }

        # Extract
        data = extract_ast(fp)
        now = int(time.time())

        # Replace in transaction
        self.conn.execute("DELETE FROM ast_files WHERE filepath=?", (fp,))
        self.conn.execute("DELETE FROM ast_nodes WHERE filepath=?", (fp,))
        self.conn.execute("DELETE FROM ast_edges WHERE filepath=?", (fp,))

        self.conn.execute(
            "INSERT INTO ast_files (filepath, file_hash, mtime, indexed_at, node_count, edge_count) "
            "VALUES (?,?,?,?,?,?)",
            (fp, fhash, Path(fp).stat().st_mtime, now,
             len(data["nodes"]), len(data["edges"])),
        )

        self.conn.executemany(
            "INSERT INTO ast_nodes (filepath, node_id, name, type, depth, lineno, end_lineno, code) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(fp, nd["id"], nd["name"], nd["type"], nd["depth"],
              nd["lineno"], nd["end_lineno"], nd["code"])
             for nd in data["nodes"]],
        )

        self.conn.executemany(
            "INSERT INTO ast_edges (filepath, from_id, to_id) VALUES (?,?,?)",
            [(fp, e["from"], e["to"]) for e in data["edges"]],
        )

        self.conn.commit()

        return {
            "file": fp,
            "fresh": False,
            "node_count": len(data["nodes"]),
            "edge_count": len(data["edges"]),
            "indexed_at": now,
        }

    def index_repo(self, root_dir: str | Path, exclude: list[str] | None = None) -> list[dict]:
        """Index all .py files under root_dir. Returns list of summaries."""
        root = Path(root_dir).resolve()
        exclude = exclude or []
        exclude_parts = set(exclude + ["__pycache__", ".venv", "venv", ".venv312",
                                        "node_modules", ".git", ".pytest_cache"])

        py_files = [
            p for p in sorted(root.rglob("*.py"))
            if not any(s in p.parts for s in exclude_parts)
            and not p.name.startswith(".")
        ]

        results = []
        for pf in py_files:
            try:
                results.append(self.index_file(pf))
            except SyntaxError:
                continue  # Skip unparseable files
        return results

    def get_ast(self, filepath: str | Path) -> dict | None:
        """Retrieve stored AST for a file. Returns {file, nodes, edges} or None."""
        fp = str(Path(filepath).resolve())
        row = self.conn.execute(
            "SELECT node_count, edge_count FROM ast_files WHERE filepath=?", (fp,)
        ).fetchone()
        if not row:
            return None

        nodes = [
            {"id": r[0], "name": r[1], "type": r[2], "depth": r[3],
             "lineno": r[4], "end_lineno": r[5], "code": r[6]}
            for r in self.conn.execute(
                "SELECT node_id, name, type, depth, lineno, end_lineno, code "
                "FROM ast_nodes WHERE filepath=? ORDER BY node_id",
                (fp,),
            )
        ]

        edges = [
            {"from": r[0], "to": r[1]}
            for r in self.conn.execute(
                "SELECT from_id, to_id FROM ast_edges WHERE filepath=? ORDER BY from_id, to_id",
                (fp,),
            )
        ]

        return {"file": fp, "nodes": nodes, "edges": edges}

    def find_nodes(self, filepath: str | Path, node_type: str | None = None,
                   name_pattern: str | None = None) -> list[dict]:
        """Query nodes by type and/or name substring."""
        fp = str(Path(filepath).resolve())
        query = "SELECT node_id, name, type, depth, lineno, end_lineno, code FROM ast_nodes WHERE filepath=?"
        params: list = [fp]

        if node_type:
            query += " AND type=?"
            params.append(node_type)
        if name_pattern:
            query += " AND name LIKE ?"
            params.append(f"%{name_pattern}%")

        query += " ORDER BY node_id"
        return [
            {"id": r[0], "name": r[1], "type": r[2], "depth": r[3],
             "lineno": r[4], "end_lineno": r[5], "code": r[6]}
            for r in self.conn.execute(query, params)
        ]

    def stats(self) -> dict:
        """Return store statistics."""
        files = self.conn.execute("SELECT COUNT(*) FROM ast_files").fetchone()[0]
        nodes = self.conn.execute("SELECT COUNT(*) FROM ast_nodes").fetchone()[0]
        edges = self.conn.execute("SELECT COUNT(*) FROM ast_edges").fetchone()[0]
        return {"files": files, "nodes": nodes, "edges": edges}


# ── Module-level convenience ────────────────────────────────────────────────

_default_store: ASTStore | None = None


def get_store(db_path: str | Path | None = None) -> ASTStore:
    global _default_store
    if _default_store is None:
        _default_store = ASTStore(db_path)
    return _default_store
