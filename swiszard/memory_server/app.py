"""
app.py — Swiszard Memory Server v2

FastAPI HTTP server. Bind: 127.0.0.1:7437

Routes:
  POST /remember          — write a memory + triggers
  POST /recall_triggers   — proactive recall (pinned + trigger-vector match, excludes deprecated)
  POST /recall_content    — on-demand recall (content-vector match, includes deprecated for forensics)
  POST /forget            — DELETE a memory by id
  POST /deprecate         — mark a memory deprecated (excluded from proactive recall)
  POST /supersede         — write new memory and link old as superseded_by
  POST /pin               — add 'always_inject' tag
  POST /unpin             — remove 'always_inject' tag
  POST /show              — fetch full row including supersede chain
  GET  /health            — liveness probe
  GET  /status            — row counts + db path
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .db import (
    get_connection,
    init_db,
    insert_memory,
    insert_trigger,
    delete_memory,
    deprecate_memory,
    supersede_memory,
    get_memory_row,
    update_tags,
    get_pinned_memory_rows,
    get_active_trigger_rows,
    get_all_memory_rows,
    count_rows,
    upsert_repo_file,
    get_repo_file_rows,
)
from .embed import embed_to_blob, top_k_rows, embed, blob_to_array, cosine_similarity
from . import embedding_rows as _er
from . import code_index as _ci

PIN_LIMIT = 5
HOME = Path.home()
PROJECT_SEARCH_ROOTS = [HOME, HOME / "Desktop", HOME / "libbieai-packs"]
PROJECT_MAX_DEPTH = 5
PROJECT_INDEX_FILE_LIMIT = 30
PROJECT_FILE_MAX_BYTES = 200_000
_PROJECT_INDEX_JOBS: set[str] = set()
_PROJECT_INDEX_LOCK = threading.Lock()
_PROJECT_INDEX_SEMAPHORE = threading.Semaphore(1)
_PROJECT_EXCLUDES = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache", "dist", "build", "target", ".mypy_cache", ".pytest_cache"}
_PROJECT_PRIORITY_NAMES = {"README.md", "readme.md", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "requirements.txt", "setup.py", "server.py", "app.py", "main.py"}


# ── trigger generation (no LLM — deterministic from content) ──────────────────

_STOP = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would could should may might shall to of in on at by for with "
    "and or but not".split()
)


def _fallback_triggers(content: str) -> list[str]:
    words = re.findall(r"[a-zA-Z]{3,}", content)
    key = [w.lower() for w in words if w.lower() not in _STOP][:8]
    if not key:
        return [content]
    noun_phrase = " ".join(key[:4])
    topic = key[0]
    triggers = [
        content,
        f"when working with {noun_phrase}",
        f"when asked about {topic}",
    ]
    if any(w in content.lower() for w in ("prefer", "use", "always", "never", "style", "format", "config", "setting")):
        triggers.append(f"when configuring or setting preferences for {topic}")
    return triggers


log = logging.getLogger("memory_server")
_conn = None


def _get_conn():
    global _conn
    if _conn is None:
        _conn = get_connection()
        init_db(_conn)
        _er.init_schema(_conn)
        if _er.needs_backfill(_conn):
            stats = _er.backfill(_conn)
            log.info("embedding_rows backfilled: %s", stats)
    return _conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("memory server v2 starting — initialising db")
    _get_conn()
    log.info("memory server ready")
    try:
        _ci.start_watcher(lambda: get_connection())
    except Exception as _exc:
        log.warning("code_index watcher start failed: %s", _exc)
    yield
    if _conn:
        _conn.close()
    log.info("memory server shut down")


app = FastAPI(title="swiszard-memory", version="2.0", lifespan=lifespan)


# ── request models ────────────────────────────────────────────────────────────

class RememberRequest(BaseModel):
    content: str
    triggers: list[str] = Field(default_factory=list)
    kind: str = "fact"
    session_id: str
    turn: int = -1
    source: str = "llm_extracted"
    tags: list[str] = Field(default_factory=list)
    ttl_seconds: int | None = None


class RecallRequest(BaseModel):
    query: str
    top_k: int = 5
    include_deprecated: bool = False  # only honored by /recall_content


class ForgetRequest(BaseModel):
    memory_id: int


class DeprecateRequest(BaseModel):
    memory_id: int
    reason: str | None = None


class SupersedeRequest(BaseModel):
    old_memory_id: int
    new_content: str
    new_triggers: list[str] = Field(default_factory=list)
    lesson: str | None = None
    session_id: str
    turn: int = -1
    source: str = "llm_extracted"
    tags: list[str] = Field(default_factory=list)


class TagRequest(BaseModel):
    memory_id: int


class ShowRequest(BaseModel):
    memory_id: int


class ListRequest(BaseModel):
    tag: str | None = None
    source: str | None = None
    include_deprecated: bool = False
    limit: int = 50
    offset: int = 0


class TagModifyRequest(BaseModel):
    memory_id: int
    tag: str


class PrepareRequest(BaseModel):
    user_message: str
    top_k: int = 6


# ── helpers ───────────────────────────────────────────────────────────────────

def _provenance(row) -> dict:
    return {
        "session_id": row["session_id"],
        "turn":       row["turn"],
        "timestamp":  row["timestamp"],
    }


def _has_key(row, key: str) -> bool:
    try:
        _ = row[key]
        return True
    except (IndexError, KeyError):
        return False




def _norm_project_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _project_tokens(text: str) -> list[str]:
    """Extract likely project/repo names from a user message, no LLM."""
    raw = set()
    # Explicit paths: include each basename component.
    for m in re.findall(r"(?:~|/home/[^\s,\"']+|\./[^\s,\"']+)", text):
        for part in Path(m.replace("~", str(HOME))).parts:
            if part and part not in {"/", "home", HOME.name}:
                raw.add(part)
    # Code/project-ish names: CamelCase, kebab, snake, dotted package-ish.
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", text):
        if tok.lower() in {"the", "and", "for", "with", "that", "this", "when", "model", "project", "repo", "repository", "swiszard", "swiszmem", "hermes"}:
            raw.add(tok)  # these are actually project names here often; keep them
        else:
            raw.add(tok)
    out = []
    seen = set()
    for tok in raw:
        n = _norm_project_token(tok)
        if 3 <= len(n) <= 40 and n not in seen:
            seen.add(n); out.append(tok)
    return out[:12]


def _is_repo_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / ".git").is_dir():
        return True
    for name in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "setup.py", "requirements.txt"):
        if (path / name).exists():
            return True
    return False


def _iter_candidate_dirs(root: Path):
    root = root.expanduser()
    if not root.exists():
        return
    stack = [(root, 0)]
    seen: set[Path] = set()
    while stack:
        cur, depth = stack.pop()
        try:
            real = cur.resolve()
        except Exception:
            continue
        if real in seen:
            continue
        seen.add(real)
        name = cur.name
        if name in _PROJECT_EXCLUDES or name.startswith(".") and name not in {".hermes"}:
            continue
        yield cur
        if depth >= PROJECT_MAX_DEPTH:
            continue
        try:
            children = [p for p in cur.iterdir() if p.is_dir()]
        except Exception:
            continue
        for child in children:
            if child.name not in _PROJECT_EXCLUDES:
                stack.append((child, depth + 1))


def _discover_project_roots(message: str) -> list[Path]:
    tokens = [_norm_project_token(t) for t in _project_tokens(message)]
    explicit_paths = []
    for m in re.findall(r"(?:~|/home/[^\s,\"']+|\./[^\s,\"']+)", message):
        p = Path(m.replace("~", str(HOME))).expanduser()
        if p.exists():
            explicit_paths.append(p if p.is_dir() else p.parent)
    roots = []
    for p in explicit_paths:
        cur = p.resolve()
        while cur != cur.parent and cur != HOME.parent:
            if _is_repo_root(cur):
                roots.append(cur); break
            cur = cur.parent
    for base in PROJECT_SEARCH_ROOTS:
        for d in _iter_candidate_dirs(base):
            n = _norm_project_token(d.name)
            if tokens and not any(t in n or n in t for t in tokens):
                continue
            if _is_repo_root(d):
                roots.append(d.resolve())
    uniq = []
    seen = set()
    for r in roots:
        if r not in seen:
            seen.add(r); uniq.append(r)
    return uniq[:8]


def _repo_id(root: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", root.name).strip("-") or "repo"


def _important_files(root: Path) -> list[Path]:
    files = []
    try:
        for name in _PROJECT_PRIORITY_NAMES:
            p = root / name
            if p.is_file():
                files.append(p)
        for p in root.rglob("*"):
            if len(files) >= PROJECT_INDEX_FILE_LIMIT:
                break
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if any(part in _PROJECT_EXCLUDES for part in rel.parts):
                continue
            if p.name in _PROJECT_PRIORITY_NAMES:
                continue
            if p.suffix.lower() not in {".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".toml", ".yaml", ".yml", ".json", ".sh", ".rs", ".go"}:
                continue
            try:
                if p.stat().st_size > PROJECT_FILE_MAX_BYTES:
                    continue
            except Exception:
                continue
            files.append(p)
    except Exception:
        pass
    return files[:PROJECT_INDEX_FILE_LIMIT]


def _summarize_file(root: Path, path: Path) -> str:
    rel = str(path.relative_to(root))
    try:
        txt = path.read_text(errors="replace")
    except Exception as exc:
        txt = f"<unreadable: {exc}>"
    first = txt[:3000]
    return f"repo={root.name}\npath={rel}\nfilename={path.name}\n---\n{first}"


def _index_repo(root: Path) -> dict:
    # One embed/index job at a time. This is a preparer, not a stampede.
    with _PROJECT_INDEX_SEMAPHORE:
        conn = get_connection()
        init_db(conn)
        rid = _repo_id(root)
        files = _important_files(root)
        indexed = 0
        try:
            for p in files:
                try:
                    st = p.stat()
                    summary = _summarize_file(root, p)
                    upsert_repo_file(conn, rid, str(p), int(st.st_mtime), summary, embed_to_blob(summary))
                    indexed += 1
                except Exception as exc:
                    log.warning("project index failed file=%s: %s", p, exc)
            return {"repo_id": rid, "root": str(root), "indexed": indexed, "candidate_files": len(files)}
        finally:
            conn.close()


def _queue_index_repo(root: Path) -> bool:
    key = str(root)
    with _PROJECT_INDEX_LOCK:
        if key in _PROJECT_INDEX_JOBS:
            return False
        _PROJECT_INDEX_JOBS.add(key)
    def _bg():
        try:
            log.info("project index start root=%s", root)
            _index_repo(root)
            log.info("project index done root=%s", root)
        finally:
            with _PROJECT_INDEX_LOCK:
                _PROJECT_INDEX_JOBS.discard(key)
    threading.Thread(target=_bg, daemon=True, name=f"swiszmem-index-{root.name[:20]}").start()
    return True


def _project_brief(conn, root: Path, query: str, top_k: int) -> dict:
    rid = _repo_id(root)
    rows = get_repo_file_rows(conn, rid)
    top_files = []
    if rows:
        scored = top_k_rows(query, rows, vec_field="vec", k=min(top_k, 8), recency_lambda=0.0)
        for sim, row in scored:
            top_files.append({
                "path": row["path"],
                "score": round(sim, 4),
                "summary": (row["summary"] or "")[:900],
            })
    return {"repo_id": rid, "root": str(root), "indexed_files": len(rows), "top_files": top_files}

# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "version": "2.0"}


@app.get("/status")
def status():
    conn = _get_conn()
    return {"counts": count_rows(conn), "version": "2.0"}




@app.post("/prepare")
def prepare(req: PrepareRequest):
    """Turn-start preparer: discover mentioned projects and warm repo_files index."""
    conn = _get_conn()
    roots = _discover_project_roots(req.user_message)
    prepared = []
    queued = []
    for root in roots:
        brief = _project_brief(conn, root, req.user_message, req.top_k)
        prepared.append(brief)
        # If empty or stale-ish, refresh in background. Cheap deterministic work;
        # embeddings run CPU-only via nomic-embed-text.
        if brief["indexed_files"] == 0:
            if _queue_index_repo(root):
                queued.append(str(root))
    return {"projects": prepared, "queued_index": queued, "tokens": _project_tokens(req.user_message)}


@app.post("/remember")
def remember(req: RememberRequest):
    conn = _get_conn()
    content_vec = embed_to_blob(req.content)
    memory_id = insert_memory(
        conn,
        content=req.content,
        content_vec=content_vec,
        kind=req.kind,
        session_id=req.session_id,
        turn=req.turn,
        source=req.source,
        ttl_seconds=req.ttl_seconds,
        tags=req.tags,
    )
    triggers = req.triggers if req.triggers else _fallback_triggers(req.content)
    # raw embedding row (memory content)
    _er.insert_row(conn, memory_id, "raw", None, req.content, content_vec)
    for trigger_text in triggers:
        trigger_vec = embed_to_blob(trigger_text)
        insert_trigger(conn, memory_id, trigger_text, trigger_vec)
        # trigger embedding row — source_id = memory_triggers.id (last insert)
        trig_src_id = conn.execute(
            "SELECT id FROM memory_triggers WHERE memory_id=? ORDER BY id DESC LIMIT 1",
            (memory_id,),
        ).fetchone()[0]
        _er.insert_row(conn, memory_id, "trigger", trig_src_id, trigger_text, trigger_vec)
    log.info("stored memory id=%d kind=%s triggers=%d", memory_id, req.kind, len(triggers))
    return {"memory_id": memory_id, "triggers_stored": len(triggers)}


@app.post("/recall_triggers")
def recall_triggers(req: RecallRequest):
    """Proactive recall: always-inject pins + similarity-matched (deprecated excluded)."""
    conn = _get_conn()

    # Always-inject pins (top of result, no similarity filter)
    pinned_rows = get_pinned_memory_rows(conn)[:PIN_LIMIT]
    pinned_ids = {row["id"] for row in pinned_rows}
    pinned_payload = [
        {
            "id":            row["id"],
            "content":       row["content"],
            "kind":          row["kind"],
            "trigger_score": 1.0,
            "matched_trigger": "<always_inject>",
            "tags":          json.loads(row["tags"] or "[]"),
            "provenance":    _provenance(row),
            "pinned":        True,
        }
        for row in pinned_rows
    ]

    # Similarity match against multi-vector embedding_rows (phase 1).
    # Per candidate memory, score = max over rows of weight[kind] * cosine(q, vec).
    # Fail loud if a row carries an unknown kind.
    rows = _er.get_active_rows(conn)
    matched_payload = []
    if rows:
        q_vec = embed(req.query)
        best: dict[int, dict] = {}
        for r in rows:
            mid = r["memory_id"]
            if mid in pinned_ids:
                continue
            kind = r["kind"]
            if kind not in _er.KIND_WEIGHTS:
                raise HTTPException(500, f"embedding_rows: unknown kind {kind!r} on memory {mid}")
            weight = _er.KIND_WEIGHTS[kind]
            r_vec = blob_to_array(bytes(r["vector"]))
            sim = cosine_similarity(q_vec, r_vec) * weight
            cur = best.get(mid)
            if cur is None or sim > cur["score"]:
                best[mid] = {
                    "score":            sim,
                    "matched_kind":     kind,
                    "matched_source":   r["source_text"],
                    "content":          r["content"],
                    "mem_kind":         r["mem_kind"],
                    "session_id":       r["session_id"],
                    "turn":             r["turn"],
                    "timestamp":        r["timestamp"],
                    "tags":             r["tags"],
                }
        top = sorted(best.items(), key=lambda kv: kv[1]["score"], reverse=True)[: req.top_k]
        matched_payload = [
            {
                "id":              mid,
                "content":         d["content"],
                "kind":            d["mem_kind"],
                "trigger_score":   round(d["score"], 4),
                "matched_trigger": d["matched_source"],
                "matched_kind":    d["matched_kind"],
                "tags":            json.loads(d["tags"] or "[]"),
                "provenance":      {
                    "session_id": d["session_id"],
                    "turn":       d["turn"],
                    "timestamp":  d["timestamp"],
                },
                "pinned":          False,
            }
            for mid, d in top
        ]

    return {"memories": pinned_payload + matched_payload}


@app.post("/recall_content")
def recall_content(req: RecallRequest):
    """On-demand recall by content vector. Includes deprecated by default for forensics."""
    conn = _get_conn()
    rows = get_all_memory_rows(conn)
    if not req.include_deprecated:
        rows = [r for r in rows if not r["deprecated"]]
    if not rows:
        return {"memories": []}

    scored = top_k_rows(req.query, rows, vec_field="content_vec", k=req.top_k)
    return {
        "memories": [
            {
                "id":            row["id"],
                "content":       row["content"],
                "kind":          row["kind"],
                "content_score": round(sim, 4),
                "tags":          json.loads(row["tags"] or "[]"),
                "provenance":    _provenance(row),
                "deprecated":    bool(row["deprecated"]),
                "superseded_by": row["superseded_by"],
                "lesson":        row["lesson"],
            }
            for sim, row in scored
        ]
    }


@app.post("/forget")
def forget(req: ForgetRequest):
    conn = _get_conn()
    if not delete_memory(conn, req.memory_id):
        raise HTTPException(status_code=404, detail="memory not found")
    return {"ok": True}


@app.post("/deprecate")
def deprecate(req: DeprecateRequest):
    conn = _get_conn()
    if not deprecate_memory(conn, req.memory_id, req.reason):
        raise HTTPException(status_code=404, detail="memory not found")
    return {"ok": True, "memory_id": req.memory_id, "reason": req.reason}


@app.post("/supersede")
def supersede(req: SupersedeRequest):
    conn = _get_conn()
    old_row = get_memory_row(conn, req.old_memory_id)
    if not old_row:
        raise HTTPException(status_code=404, detail="old_memory_id not found")

    # Insert new memory
    content_vec = embed_to_blob(req.new_content)
    new_id = insert_memory(
        conn,
        content=req.new_content,
        content_vec=content_vec,
        kind=old_row["kind"],
        session_id=req.session_id,
        turn=req.turn,
        source=req.source,
        ttl_seconds=None,
        tags=req.tags,
    )
    triggers = req.new_triggers if req.new_triggers else _fallback_triggers(req.new_content)
    _er.insert_row(conn, new_id, "raw", None, req.new_content, content_vec)
    for t in triggers:
        tvec = embed_to_blob(t)
        insert_trigger(conn, new_id, t, tvec)
        trig_src_id = conn.execute(
            "SELECT id FROM memory_triggers WHERE memory_id=? ORDER BY id DESC LIMIT 1",
            (new_id,),
        ).fetchone()[0]
        _er.insert_row(conn, new_id, "trigger", trig_src_id, t, tvec)

    supersede_memory(conn, req.old_memory_id, new_id, req.lesson)
    log.info("superseded memory %d -> %d", req.old_memory_id, new_id)
    return {
        "new_memory_id": new_id,
        "old_memory_id": req.old_memory_id,
        "triggers_stored": len(triggers),
    }


def _modify_tags(memory_id: int, tag: str, add: bool) -> dict:
    conn = _get_conn()
    row = get_memory_row(conn, memory_id)
    if not row:
        raise HTTPException(status_code=404, detail="memory not found")
    tags = json.loads(row["tags"] or "[]")
    already_has_tag = tag in tags

    if add and tag == "always_inject" and not already_has_tag:
        pinned_rows = get_pinned_memory_rows(conn)
        if len(pinned_rows) >= PIN_LIMIT:
            pinned_ids = [r["id"] for r in pinned_rows]
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "pin_limit_reached",
                    "limit": PIN_LIMIT,
                    "pinned_ids": pinned_ids,
                    "message": f"at most {PIN_LIMIT} memories may be pinned; unpin one before pinning another",
                },
            )

    if add and not already_has_tag:
        tags.append(tag)
    elif not add and already_has_tag:
        tags.remove(tag)
    update_tags(conn, memory_id, tags)
    return {"ok": True, "memory_id": memory_id, "tags": tags}


@app.post("/pin")
def pin(req: TagRequest):
    return _modify_tags(req.memory_id, "always_inject", add=True)


@app.post("/unpin")
def unpin(req: TagRequest):
    return _modify_tags(req.memory_id, "always_inject", add=False)


@app.post("/show")
def show(req: ShowRequest):
    conn = _get_conn()
    row = get_memory_row(conn, req.memory_id)
    if not row:
        raise HTTPException(status_code=404, detail="memory not found")

    # Walk supersede chain forward
    chain = []
    cursor = row
    while cursor and cursor["superseded_by"]:
        chain.append(cursor["superseded_by"])
        cursor = get_memory_row(conn, cursor["superseded_by"])
        if cursor and cursor["id"] in chain[:-1]:
            break  # cycle protection

    return {
        "id":            row["id"],
        "content":       row["content"],
        "kind":          row["kind"],
        "tags":          json.loads(row["tags"] or "[]"),
        "provenance":    _provenance(row),
        "deprecated":    bool(row["deprecated"]),
        "deprecated_reason": row["deprecated_reason"],
        "superseded_by": row["superseded_by"],
        "lesson":        row["lesson"],
        "superseded_chain": chain,
    }


# ── tag/untag/list (browse without semantic recall) ──────────────────────────

@app.post("/tag")
def tag(req: TagModifyRequest):
    return _modify_tags(req.memory_id, req.tag, add=True)


@app.post("/untag")
def untag(req: TagModifyRequest):
    return _modify_tags(req.memory_id, req.tag, add=False)


@app.post("/list")
def list_memories(req: ListRequest):
    """Deterministic browse by tag/source. No embedding, no similarity."""
    import sqlite3
    conn = _get_conn()
    where = []
    params = []
    if not req.include_deprecated:
        where.append("deprecated = 0")
    if req.tag:
        where.append("tags LIKE ?")
        params.append(f"%{req.tag}%")
    if req.source:
        where.append("source = ?")
        params.append(req.source)
    sql = "SELECT id, content, kind, source, tags, deprecated, deprecated_reason, superseded_by, timestamp FROM memories"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([req.limit, req.offset])
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        out.append({
            "id":         r["id"],
            "content":    r["content"],
            "kind":       r["kind"],
            "source":     r["source"],
            "tags":       json.loads(r["tags"] or "[]"),
            "deprecated": bool(r["deprecated"]),
            "superseded_by": r["superseded_by"],
            "timestamp":  r["timestamp"],
        })
    # total count for pagination
    count_sql = "SELECT COUNT(*) FROM memories"
    if where:
        count_sql += " WHERE " + " AND ".join(where)
    total = conn.execute(count_sql, params[:-2] if where else []).fetchone()[0]
    return {"memories": out, "total": total, "returned": len(out), "offset": req.offset, "limit": req.limit}


# ── trigger CRUD (additive, append-only by default) ─────────────────────────

class TriggerListRequest(BaseModel):
    memory_id: int

class TriggerAddRequest(BaseModel):
    memory_id: int
    trigger_text: str

class TriggerRemoveRequest(BaseModel):
    trigger_id: int

@app.post("/trigger_list")
def trigger_list(req: TriggerListRequest):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, trigger_text FROM memory_triggers WHERE memory_id = ? ORDER BY id",
        (req.memory_id,),
    ).fetchall()
    return {"memory_id": req.memory_id, "triggers": [{"id": r["id"], "text": r["trigger_text"]} for r in rows]}

@app.post("/trigger_add")
def trigger_add(req: TriggerAddRequest):
    conn = _get_conn()
    row = conn.execute("SELECT id FROM memories WHERE id = ?", (req.memory_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="memory not found")
    text = req.trigger_text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="trigger_text empty")
    vec = embed_to_blob(text)
    insert_trigger(conn, req.memory_id, text, vec)
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"ok": True, "memory_id": req.memory_id, "trigger_id": new_id, "trigger_text": text}

@app.post("/trigger_remove")
def trigger_remove(req: TriggerRemoveRequest):
    conn = _get_conn()
    cur = conn.execute("DELETE FROM memory_triggers WHERE id = ?", (req.trigger_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="trigger not found")
    return {"ok": True, "trigger_id": req.trigger_id}

# ── code index models ─────────────────────────────────────────────────────────

class CodeIndexAddRequest(BaseModel):
    root: str

class CodeIndexRemoveRequest(BaseModel):
    root: str

class CodeSearchRequest(BaseModel):
    query: str
    top_k: int = 5
    repo_id: str | None = None


@app.post("/code/index_add")
def code_index_add(req: CodeIndexAddRequest):
    root = Path(req.root).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(400, f"not a directory: {root}")
    # Kick off in background; watcher will keep refreshing it forever.
    return _ci.kickoff_index(lambda: get_connection(), root)


@app.get("/code/index_status")
def code_index_status(root: str):
    return _ci.job_status(Path(root).expanduser().resolve())


@app.post("/code/index_remove")
def code_index_remove(req: CodeIndexRemoveRequest):
    root = Path(req.root).expanduser().resolve()
    conn = _get_conn()
    return _ci.remove_root(conn, root)


@app.get("/code/index_list")
def code_index_list():
    conn = _get_conn()
    return {"roots": _ci.list_roots(conn)}


@app.post("/code/search")
def code_search(req: CodeSearchRequest):
    conn = _get_conn()
    hits = _ci.search(conn, req.query, top_k=req.top_k, repo_id=req.repo_id)
    return {"query": req.query, "hits": hits}



# ── projects (project-manager substrate) ─────────────────────────────────────
from swiszproj import server_engine as _pm
from swiszproj import pm_backup

_PM_INIT_DONE = False
_PM_INIT_LOCK = threading.Lock()

def _ensure_pm():
    global _PM_INIT_DONE
    if _PM_INIT_DONE:
        return
    with _PM_INIT_LOCK:
        if _PM_INIT_DONE:
            return
        _pm.init_schema(_get_conn())
        try:
            _pm.migrate_legacy_nodes(_get_conn())
        except Exception as exc:
            log.warning("pm migrate failed: %s", exc)
        _PM_INIT_DONE = True


class PMCreateRequest(BaseModel):
    name: str


class PMAddNodeRequest(BaseModel):
    project: str
    body: str
    kind: str = "objective"
    state: str = "proposed"
    parent_id: int | None = None
    tags: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    title: str | None = None
    scan_conflicts: bool = True


class PMTreeRequest(BaseModel):
    project: str


class PMInjectRequest(BaseModel):
    query: str
    top_k: int = 4
    active_project: str | None = None


class PMConflictsRequest(BaseModel):
    project: str | None = None


class PMResolveRequest(BaseModel):
    conflict_id: int
    resolution: str  # free text: e.g. "merge", "supersede", "both-valid", reason


class PMProposeParentRequest(BaseModel):
    project: str
    body: str
    top_k: int = 5


# old /project/create replaced by dedup-aware version below


@app.get("/project/list")
def pm_list():
    _ensure_pm()
    return {"projects": _pm.list_projects(_get_conn())}


@app.post("/project/add_node")
def pm_add_node(req: PMAddNodeRequest):
    _ensure_pm()
    conn = _get_conn()
    pid = _pm.get_or_create_project(conn, req.project)
    try:
        pm_backup.log_mutation("INSERT", "pm_node", 0,
                               new_row={"project": req.project, "kind": req.kind,
                                        "state": req.state, "title": req.title,
                                        "body_preview": req.body[:200]},
                               metadata={"endpoint": "/project/add_node"})
        node_id = _pm.insert_node(
            conn, pid, req.body, kind=req.kind, state=req.state,
            parent_id=req.parent_id, tags=req.tags, title=req.title,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    for t in req.triggers:
        if t.strip():
            _pm.insert_trigger(conn, node_id, t.strip())
    queued = []
    if req.scan_conflicts:
        def _bg():
            try:
                _pm.scan_conflicts(get_connection(), node_id)
            except Exception as exc:
                log.warning("pm conflict scan failed for node %s: %s", node_id, exc)
        threading.Thread(target=_bg, daemon=True, name="pm-conflict-scan").start()
    return {"node_id": node_id, "project_id": pid, "queued_conflict_scan": req.scan_conflicts}


@app.post("/project/tree")
def pm_tree(req: PMTreeRequest):
    _ensure_pm()
    conn = _get_conn()
    proj = _pm.get_project_by_name(conn, req.project)
    if not proj:
        raise HTTPException(404, f"unknown project: {req.project}")
    return {"project": dict(proj), "nodes": _pm.project_tree(conn, proj["id"])}


@app.post("/project/inject")
def pm_inject(req: PMInjectRequest):
    _ensure_pm()
    conn = _get_conn()
    active_id = None
    if req.active_project:
        proj = _pm.get_project_by_name(conn, req.active_project)
        if proj:
            active_id = proj["id"]
    frames = _pm.inject_frames(conn, req.query, top_k=req.top_k,
                               active_project_id=active_id)
    return {"frames": frames}


@app.post("/project/conflicts")
def pm_conflicts(req: PMConflictsRequest):
    _ensure_pm()
    conn = _get_conn()
    pid = None
    if req.project:
        proj = _pm.get_project_by_name(conn, req.project)
        if proj:
            pid = proj["id"]
    return {"conflicts": _pm.open_conflicts(conn, pid)}


@app.post("/project/resolve")
def pm_resolve(req: PMResolveRequest):
    _ensure_pm()
    ok = _pm.resolve_conflict(_get_conn(), req.conflict_id, req.resolution)
    if not ok:
        raise HTTPException(404, f"unknown conflict id: {req.conflict_id}")
    return {"ok": True}


@app.post("/project/propose_parent")
def pm_propose_parent(req: PMProposeParentRequest):
    _ensure_pm()
    conn = _get_conn()
    proj = _pm.get_project_by_name(conn, req.project)
    if not proj:
        return {"candidates": []}
    return {"candidates": _pm.propose_parent(conn, proj["id"], req.body, top_k=req.top_k)}


# ── state transition ─────────────────────────────────────────────────────

class PMTransitionRequest(BaseModel):
    node_id: int
    state: str  # kind-specific states: see projects.py KIND_STATES


@app.post("/project/transition")
def pm_transition(req: PMTransitionRequest):
    _ensure_pm()
    conn = _get_conn()
    # Pre-fetch node so we can return valid_next on bad transitions without HTTP 400.
    # HTTP 400 trips Hermes circuit breaker even when the server is healthy.
    node = _pm.get_node(conn, req.node_id)
    if not node:
        return {"ok": False, "error": f"node {req.node_id} not found", "valid_next": []}
    old_state = node["state"]
    valid_next = sorted(_pm.VALID_TRANSITIONS.get(old_state, set()))
    if req.state not in (valid_next if valid_next else []):
        return {"ok": False, "error": f"invalid transition {old_state} -> {req.state}", "valid_next": valid_next}
    try:
        pm_backup.log_mutation("UPDATE", "pm_node", req.node_id,
                               new_row={"state": req.state},
                               metadata={"endpoint": "/project/transition"})
        result = _pm.state_transition(conn, req.node_id, req.state)
        return {**result, "ok": True}
    except ValueError as e:
        return {"ok": False, "error": str(e), "valid_next": valid_next}



# ── single node fetch ─────────────────────────────────────────────────────

@app.get("/project/node")
def pm_get_node(node_id: int):
    """Fetch a single node by id. Returns full row or 404."""
    _ensure_pm()
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, project_id, parent_id, kind, state, title, body, created, updated, "
        "COALESCE(tags, \'[]\') as tags FROM pm_node WHERE id=?",
        (node_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"node {node_id} not found")
    return dict(row)


# ── subtree fetch ─────────────────────────────────────────────────────────

class PMSubtreeRequest(BaseModel):
    root_id: int
    show_all: bool = True


@app.post("/project/subtree")
def pm_subtree(req: PMSubtreeRequest):
    """Return all descendants of root_id as a flat list (root included)."""
    _ensure_pm()
    conn = _get_conn()
    root = _pm.get_node(conn, req.root_id)
    if not root:
        raise HTTPException(404, f"node {req.root_id} not found")
    # Get project_id from root node, fetch all project nodes, walk subtree
    project_id = root["project_id"]
    all_nodes = _pm.project_tree(conn, project_id)
    by_parent: dict = {}
    node_map: dict = {}
    for n in all_nodes:
        node_map[n["id"]] = n
        by_parent.setdefault(n.get("parent_id"), []).append(n)

    result = []
    def _walk(nid: int):
        n = node_map.get(nid)
        if n:
            result.append(n)
        for child in by_parent.get(nid, []):
            _walk(child["id"])
    _walk(req.root_id)
    return {"root_id": req.root_id, "nodes": result, "total": len(result)}


# ── project compass (status) ──────────────────────────────────────────────

class PMStatusRequest(BaseModel):
    project: str
    max_bottlenecks: int = 5


@app.post("/project/status")
def pm_status(req: PMStatusRequest):
    _ensure_pm()
    conn = _get_conn()
    proj = _pm.get_project_by_name(conn, req.project)
    if not proj:
        raise HTTPException(404, f"unknown project: {req.project}")
    return _pm.project_status(conn, proj["id"], max_bottlenecks=req.max_bottlenecks)


# ── safety endpoints ──────────────────────────────────────────────────────

class PMDeleteRequest(BaseModel):
    node_id: int
    confirmation_token: str
    expected_title: str = ""


@app.post("/project/delete_node")
def pm_delete_node(req: PMDeleteRequest):
    _ensure_pm()
    try:
        from swiszproj import pm_backup
        pm_backup.log_mutation("DELETE", "pm_node", req.node_id,
                               metadata={"endpoint": "/project/delete_node",
                                         "expected_title": req.expected_title[:80]})
        result = _pm.delete_node(_get_conn(), req.node_id,
                                 req.confirmation_token, req.expected_title)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


class PMReparentRequest(BaseModel):
    node_id: int
    new_parent_id: int


@app.post("/project/reparent")
def pm_reparent(req: PMReparentRequest):
    _ensure_pm()
    try:
        from swiszproj import pm_backup
        pm_backup.log_mutation("UPDATE", "pm_node", req.node_id,
                               new_row={"parent_id": req.new_parent_id},
                               metadata={"endpoint": "/project/reparent"})
        result = _pm.reparent_node(_get_conn(), req.node_id, req.new_parent_id)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


# Patch /project/create to use dedup-aware get_or_create
@app.post("/project/create")
def pm_create(req: PMCreateRequest):
    _ensure_pm()
    conn = _get_conn()
    pid, created = _pm.get_or_create_project_dedup(conn, req.name)
    from swiszproj import pm_backup
    if created:
        pm_backup.log_mutation("INSERT", "pm_project", pid,
                               new_row={"name": req.name},
                               metadata={"endpoint": "/project/create"})
    return {"id": pid, "name": req.name, "created": created}

class PMRenameRequest(BaseModel):
    old_name: str
    new_name: str

@app.post("/project/rename")
def pm_rename(req: PMRenameRequest):
    _ensure_pm()
    conn = _get_conn()
    try:
        result = _pm.rename_project(conn, req.old_name, req.new_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from swiszproj import pm_backup
    pm_backup.log_mutation("UPDATE", "pm_project", result["project_id"],
                           old_row={"name": req.old_name},
                           new_row={"name": req.new_name},
                           metadata={"endpoint": "/project/rename"})
    return result

# ── PM orientation endpoint (for proactive injection) ─────────────────────

class PMOrientRequest(BaseModel):
    project: str = "swiszard"

@app.post("/project/orient")
def pm_orient(req: PMOrientRequest):
    """Return a compact orientation block for proactive injection.
    Format: PM: swiszard - North star: active - 74/101 done (16 frontier) - modules: hermes(3a) swiszcode(5a) swisznet(3a) - trust: 95% (0 orphans, 2 stale)"""
    _ensure_pm()
    conn = _get_conn()
    proj = _pm.get_project_by_name(conn, req.project)
    if not proj:
        raise HTTPException(404, f"unknown project: {req.project}")
    
    pid = proj["id"]
    status = _pm.project_status(conn, pid)
    tree = _pm.project_tree(conn, pid)
    nodes = {n["id"]: n for n in tree}
    
    # Module branches
    modules = {"swiszproj":42, "swiszmem":43, "swiszcli":44, "swiszcode":52, "swisznet":74, "hermes":222}
    mod_counts = {}
    for name, root_id in modules.items():
        if root_id in nodes:
            active = done = blocked = 0
            def count_states(nid):
                nonlocal active, done, blocked
                n = nodes.get(nid)
                if not n:
                    return
                s = n["state"]
                if s == "active": active += 1
                elif s in ("done","satisfied"): done += 1
                elif s == "blocked": blocked += 1
                for child in [c for c in tree if c.get("parent_id") == nid]:
                    count_states(child["id"])
            count_states(root_id)
            if active > 0:
                mod_counts[name] = f"{active}a"
    
    mod_str = " ".join(f"{k}({v})" for k, v in mod_counts.items())
    
    # Health: count orphans
    orphan_ids = [n["id"] for n in tree if n.get("parent_id") and n["parent_id"] not in nodes]
    # Count stale frontier (>7 days)
    now = int(time.time())
    stale = [f for f in status.get("frontier", []) if now - f["updated"] > 7*86400]
    # Count empty-body nodes
    empty_bodies = [n["id"] for n in tree if n.get("body", "").strip() == "" and n["state"] == "active"]
    
    real_issues = len(orphan_ids) + len(stale)
    trust = max(0, 100 - real_issues * 5)
    
    block = f'PM: {req.project} - {status["summary"]} - modules: {mod_str or "none active"} - trust: {trust}%'
    if issues:
        block += f' ({len(orphan_ids)} orphans, {len(stale)} stale, {len(empty_bodies)} empty)'
    
    return {"block": block, "trust": trust, "issues": {"orphans": orphan_ids, "stale": len(stale), "empty": empty_bodies}}

# ── PM health check ───────────────────────────────────────────────────────

@app.post("/project/recall_triggers")
def pm_recall_triggers(req: PMInjectRequest):
    """Return pm_nodes whose trigger phrases most closely match query."""
    _ensure_pm()
    conn = _get_conn()
    from swiszproj.server_engine import embed_to_blob, blob_to_array, cosine_similarity

    active_id = None
    if req.active_project:
        proj = _pm.get_project_by_name(conn, req.active_project)
        if proj:
            active_id = proj["id"]

    qv = blob_to_array(embed_to_blob(req.query))
    trig_rows = conn.execute(
        "SELECT t.id, t.node_id, t.text, t.vec, n.title, n.kind, n.state, n.project_id "
        "FROM pm_trigger t JOIN pm_node n ON n.id=t.node_id "
        "WHERE n.state NOT IN ('dead','deprecated','done','archived','satisfied','superseded','invalidated','reverted','removed','abandoned')"
    ).fetchall()

    scored = {}
    for tr in trig_rows:
        if active_id and tr["project_id"] != active_id:
            continue
        score = cosine_similarity(qv, blob_to_array(bytes(tr["vec"])))
        nid = tr["node_id"]
        if nid not in scored or score > scored[nid]["score"]:
            scored[nid] = {
                "node_id": nid,
                "trigger_text": tr["text"],
                "score": round(score, 4),
                "title": tr["title"],
                "kind": tr["kind"],
                "state": tr["state"],
            }

    results = sorted(scored.values(), key=lambda x: -x["score"])[:req.top_k]
    return {"query": req.query, "matches": results}


@app.get("/project/health")
def pm_health_endpoint():
    """Return PM tree health: orphans, stale nodes, empty-body nodes, trust score."""
    _ensure_pm()
    conn = _get_conn()
    tree = _pm.project_tree(conn, 1)  # swiszard
    nodes = {n["id"]: n for n in tree}
    
    orphans = [n for n in tree if n.get("parent_id") and n["parent_id"] not in nodes]
    now = int(time.time())
    stale = [n for n in tree if n["state"] == "active" and now - n["updated"] > 7*86400]
    empty = [n for n in tree if (n.get("body") or "").strip() == "" and n["state"] == "active"]
    wrong_state = [n for n in tree if n["kind"] in ("task","artifact") and n["state"] == "satisfied"]
    
    critical = len(orphans) + len(stale) + len(wrong_state)
    trust = max(0, 100 - critical * 5)
    
    return {
        "trust": trust,
        "issues": {
            "orphans": [{"id": n["id"], "title": n["title"][:60], "parent_id": n["parent_id"]} for n in orphans[:5]],
            "stale": len(stale),
            "empty_bodies": len(empty),
            "wrong_state": len(wrong_state),
        }
    }



@app.get('/router/match')
def router_match(task: str, dry_run: bool = True):
    from swiszard.router import swiszard_do as _route
    if not task or not task.strip():
        raise HTTPException(status_code=400, detail='task required')
    result = _route(task.strip(), dry_run=True)
    handler = None
    if '[dry-run] would route to:' in result:
        handler = result.split('would route to:',1)[1].strip()
    return {'task': task, 'handler': handler, 'raw': result}
