"""code_index.py — AST-aware code chunk indexer with embeddings.

Tables (created lazily):
  indexed_roots(root TEXT PK, repo_id TEXT, added_at INT, last_scan_at INT, active INT)
  code_chunks(id INT PK, repo_id TEXT, path TEXT, kind TEXT, name TEXT,
              start_line INT, end_line INT, sha TEXT, content TEXT, vec BLOB, mtime INT)
"""
from __future__ import annotations

import ast
import hashlib
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

import numpy as np

from .embed import embed, embed_to_blob, blob_to_array, cosine_similarity

log = logging.getLogger("swiszmem.code_index")

CHUNK_MAX_LINES = 80
CHUNK_OVERLAP = 10
MAX_FILE_BYTES = 400_000
SCAN_INTERVAL_SEC = 60

CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
    ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift",
    ".scala", ".lua", ".sh", ".bash", ".zsh", ".sql", ".md", ".rst",
    ".yaml", ".yml", ".toml", ".json", ".html", ".css", ".vue", ".svelte",
}
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".cache", "target", ".pytest_cache",
    ".mypy_cache", ".tox", "vendor", ".idea", ".vscode",
}


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS indexed_roots (
            root         TEXT PRIMARY KEY,
            repo_id      TEXT NOT NULL,
            added_at     INTEGER NOT NULL,
            last_scan_at INTEGER NOT NULL DEFAULT 0,
            active       INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS code_chunks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id     TEXT NOT NULL,
            path        TEXT NOT NULL,
            kind        TEXT NOT NULL,
            name        TEXT NOT NULL,
            start_line  INTEGER NOT NULL,
            end_line    INTEGER NOT NULL,
            sha         TEXT NOT NULL,
            content     TEXT NOT NULL,
            vec         BLOB NOT NULL,
            mtime       INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_code_chunks_repo ON code_chunks(repo_id);
        CREATE INDEX IF NOT EXISTS idx_code_chunks_path ON code_chunks(path);
    """)
    conn.commit()


def _repo_id_for(root: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", root.name).strip("-") or "repo"


def _iter_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in CODE_EXTS:
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield p


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _chunk_python(text: str) -> list[dict]:
    """AST-chunk python: each top-level def/class is one chunk."""
    chunks: list[dict] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _chunk_lines(text, kind="py_fallback")
    lines = text.splitlines()
    covered: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            body = "\n".join(lines[start - 1:end])
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            chunks.append({
                "kind": kind, "name": node.name,
                "start_line": start, "end_line": end, "content": body,
            })
            for i in range(start, end + 1):
                covered.add(i)
    # Module-level remainder (imports, constants, scripts) → one chunk
    leftover = [(i + 1, l) for i, l in enumerate(lines) if (i + 1) not in covered and l.strip()]
    if leftover:
        body = "\n".join(l for _, l in leftover)
        chunks.append({
            "kind": "module", "name": "<module>",
            "start_line": leftover[0][0], "end_line": leftover[-1][0],
            "content": body[:8000],
        })
    return chunks


def _chunk_lines(text: str, kind: str = "lines") -> list[dict]:
    lines = text.splitlines()
    if not lines:
        return []
    chunks = []
    step = CHUNK_MAX_LINES - CHUNK_OVERLAP
    for start in range(0, len(lines), step):
        end = min(start + CHUNK_MAX_LINES, len(lines))
        body = "\n".join(lines[start:end])
        if not body.strip():
            continue
        chunks.append({
            "kind": kind, "name": f"L{start+1}-{end}",
            "start_line": start + 1, "end_line": end, "content": body,
        })
        if end >= len(lines):
            break
    return chunks


def chunk_file(path: Path, text: str) -> list[dict]:
    if path.suffix.lower() == ".py":
        return _chunk_python(text)
    return _chunk_lines(text, kind=path.suffix.lstrip(".") or "txt")


def _embed_text(path: Path, ch: dict) -> bytes:
    header = f"path={path}\nkind={ch['kind']} name={ch['name']} lines={ch['start_line']}-{ch['end_line']}\n---\n"
    body = ch["content"][:4000]
    return embed_to_blob(header + body)


def index_root(conn: sqlite3.Connection, root: Path) -> dict:
    """Full (re)index of one root. Idempotent — replaces all chunks for paths under root."""
    _init_tables(conn)
    rid = _repo_id_for(root)
    indexed = 0
    files_seen = 0
    chunks_total = 0
    skipped = 0
    seen_paths: set[str] = set()
    for p in _iter_files(root):
        files_seen += 1
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("read failed %s: %s", p, exc)
            continue
        mtime = int(p.stat().st_mtime)
        # Check if any chunk for this path is up-to-date
        existing = conn.execute(
            "SELECT mtime FROM code_chunks WHERE path=? LIMIT 1", (str(p),)
        ).fetchone()
        if existing and existing["mtime"] == mtime:
            seen_paths.add(str(p))
            skipped += 1
            continue
        chunks = chunk_file(p, txt)
        if not chunks:
            continue
        conn.execute("DELETE FROM code_chunks WHERE path=?", (str(p),))
        for ch in chunks:
            sha = _sha(ch["content"])
            try:
                vec = _embed_text(p, ch)
            except Exception as exc:
                log.warning("embed failed %s %s: %s", p, ch["name"], exc)
                continue
            conn.execute(
                """INSERT INTO code_chunks
                   (repo_id, path, kind, name, start_line, end_line, sha, content, vec, mtime)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (rid, str(p), ch["kind"], ch["name"], ch["start_line"],
                 ch["end_line"], sha, ch["content"], vec, mtime),
            )
            chunks_total += 1
        seen_paths.add(str(p))
        indexed += 1
    # Purge chunks whose paths no longer exist under this root
    purged = 0
    cur = conn.execute(
        "SELECT DISTINCT path FROM code_chunks WHERE repo_id=? AND path LIKE ?",
        (rid, str(root) + "%"),
    )
    stale = [r["path"] for r in cur if r["path"] not in seen_paths]
    for sp in stale:
        if not Path(sp).exists():
            conn.execute("DELETE FROM code_chunks WHERE path=?", (sp,))
            purged += 1
    now = int(time.time())
    conn.execute(
        """INSERT INTO indexed_roots (root, repo_id, added_at, last_scan_at, active)
           VALUES (?, ?, ?, ?, 1)
           ON CONFLICT(root) DO UPDATE SET last_scan_at=excluded.last_scan_at, active=1""",
        (str(root), rid, now, now),
    )
    conn.commit()
    return {
        "root": str(root), "repo_id": rid, "files_seen": files_seen,
        "files_indexed": indexed, "files_unchanged": skipped,
        "chunks_total": chunks_total, "purged_stale": purged,
    }


def remove_root(conn: sqlite3.Connection, root: Path) -> dict:
    _init_tables(conn)
    rid = _repo_id_for(root)
    n = conn.execute(
        "DELETE FROM code_chunks WHERE path LIKE ?", (str(root) + "%",)
    ).rowcount
    conn.execute("DELETE FROM indexed_roots WHERE root=?", (str(root),))
    conn.commit()
    return {"root": str(root), "repo_id": rid, "chunks_deleted": n}


def list_roots(conn: sqlite3.Connection) -> list[dict]:
    _init_tables(conn)
    rows = conn.execute(
        """SELECT r.root, r.repo_id, r.added_at, r.last_scan_at, r.active,
                  COUNT(c.id) AS chunks
             FROM indexed_roots r
             LEFT JOIN code_chunks c ON c.repo_id = r.repo_id
            GROUP BY r.root
            ORDER BY r.added_at DESC""",
    ).fetchall()
    return [dict(r) for r in rows]


def search(conn: sqlite3.Connection, query: str, top_k: int = 8,
           repo_id: str | None = None) -> list[dict]:
    _init_tables(conn)
    qvec = embed(query).astype(np.float32)
    where = ""
    args: list = []
    if repo_id:
        where = "WHERE repo_id=?"
        args.append(repo_id)
    cur = conn.execute(
        f"SELECT id, repo_id, path, kind, name, start_line, end_line, content, vec FROM code_chunks {where}",
        args,
    )
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in cur:
        try:
            v = blob_to_array(row["vec"])
        except Exception:
            continue
        sim = float(cosine_similarity(qvec, v))
        scored.append((sim, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for sim, row in scored[:top_k]:
        out.append({
            "score": round(sim, 4),
            "repo_id": row["repo_id"],
            "path": row["path"],
            "kind": row["kind"],
            "name": row["name"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "content": row["content"],
        })
    return out


# ── background watcher ────────────────────────────────────────────────────────
_WATCHER_THREAD: threading.Thread | None = None
_WATCHER_STOP = threading.Event()


def start_watcher(get_conn) -> None:
    """Spawn a single background thread that rescans active roots every N sec."""
    global _WATCHER_THREAD
    if _WATCHER_THREAD and _WATCHER_THREAD.is_alive():
        return

    def _loop():
        log.info("code_index watcher started (interval=%ds)", SCAN_INTERVAL_SEC)
        while not _WATCHER_STOP.is_set():
            try:
                conn = get_conn()
                try:
                    rows = list_roots(conn)
                    for r in rows:
                        if not r["active"]:
                            continue
                        root = Path(r["root"])
                        if not root.is_dir():
                            log.warning("indexed root missing: %s", root)
                            continue
                        try:
                            stats = index_root(conn, root)
                            if stats["files_indexed"] or stats["purged_stale"]:
                                log.info("watcher rescan %s: %s", root, stats)
                        except Exception as exc:
                            log.warning("watcher rescan failed %s: %s", root, exc)
                finally:
                    conn.close()
            except Exception as exc:
                log.warning("watcher loop error: %s", exc)
            _WATCHER_STOP.wait(SCAN_INTERVAL_SEC)

    _WATCHER_THREAD = threading.Thread(target=_loop, daemon=True, name="swiszmem-code-watcher")
    _WATCHER_THREAD.start()

# ── async kickoff (don't block HTTP handler) ─────────────────────────────────
_INDEX_JOBS_LOCK = threading.Lock()
_INDEX_JOBS: dict[str, dict] = {}  # root -> {"status": "running"|"done"|"error", "stats": {}, "error": str}


def kickoff_index(get_conn, root: Path) -> dict:
    """Register the root as active immediately, then run the actual index in a thread."""
    key = str(root)
    with _INDEX_JOBS_LOCK:
        if _INDEX_JOBS.get(key, {}).get("status") == "running":
            return {"root": key, "status": "already_running"}
        _INDEX_JOBS[key] = {"status": "running", "stats": None, "error": None}
    # Pre-register the root so /index list shows it immediately
    conn = get_conn()
    try:
        _init_tables(conn)
        rid = _repo_id_for(root)
        now = int(time.time())
        conn.execute(
            """INSERT INTO indexed_roots (root, repo_id, added_at, last_scan_at, active)
               VALUES (?, ?, ?, 0, 1)
               ON CONFLICT(root) DO UPDATE SET active=1""",
            (key, rid, now),
        )
        conn.commit()
    finally:
        conn.close()

    def _bg():
        try:
            conn2 = get_conn()
            try:
                stats = index_root(conn2, root)
                with _INDEX_JOBS_LOCK:
                    _INDEX_JOBS[key] = {"status": "done", "stats": stats, "error": None}
                log.info("kickoff_index done %s: %s", root, stats)
            finally:
                conn2.close()
        except Exception as exc:
            log.warning("kickoff_index failed %s: %s", root, exc)
            with _INDEX_JOBS_LOCK:
                _INDEX_JOBS[key] = {"status": "error", "stats": None, "error": str(exc)}
    threading.Thread(target=_bg, daemon=True, name=f"swiszmem-codeidx-{root.name[:16]}").start()
    return {"root": key, "status": "queued", "repo_id": _repo_id_for(root)}


def job_status(root: Path) -> dict:
    with _INDEX_JOBS_LOCK:
        return dict(_INDEX_JOBS.get(str(root), {"status": "unknown"}))

