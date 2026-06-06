"""
router.py — Deterministic intent dispatcher for swiszard.

Architecture:
    1. Rule-based routing (regex/keywords)
    2. TF-IDF cosine routing over example phrases
    3. Embed task with CPU-only all-MiniLM-L6-v2 and compare to examples
    4. Route rules (for TF-IDF and embeddings):
         - top match sim > threshold AND success_count > fail_count → route
         - top-2 different handlers and both > ambig_threshold      → clarification
         - otherwise                                                → return no-match
    5. Emit real-time narration on stderr throughout
    6. --dry-run: print which handler would be picked, do not execute

Additional MCP hook:
    swiszard_feedback(task, handler_used, was_good) — call from parent loop.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections import Counter
from pathlib import Path

from .narrate import narrate
from .db import (
    get_connection,
    init_db,
    get_all_examples,
    insert_example,
    increment_success,
    increment_fail,
)
from .embeddings import embed, embed_to_blob, blob_to_array, cosine_similarity
from .seeds import SEED_EXAMPLES
from .handlers import (
    handler_file_read,
    handler_file_find,
    handler_file_write,
    handler_shell,
    handler_web_search,
    handler_memory,
    handler_edit,
    handler_skill,
    handler_ast_transform,
    handler_ast_pin,
)

# ── constants ─────────────────────────────────────────────────────────────────

TOP_K = 5
ROUTE_THRESHOLD = 0.75  # sim > this → route (if handler is trusted)
AMBIG_THRESHOLD = 0.70  # sim > this for both top-2 different handlers → clarify
TFIDF_ROUTE_THRESHOLD = 0.45
TFIDF_AMBIG_THRESHOLD = 0.35
SEED_MARKER = Path(os.path.expanduser("~/.hermes/swiszard/.seeded"))

# ── handler registry ──────────────────────────────────────────────────────────

HANDLER_MAP: dict[str, callable] = {
    "handler_file_read": handler_file_read,
    "handler_file_find": handler_file_find,
    "handler_file_write": handler_file_write,
    "handler_shell": handler_shell,
    "handler_web_search": handler_web_search,
    "handler_memory": handler_memory,
    "handler_edit": handler_edit,
    "handler_skill": handler_skill,
    "handler_ast_transform": handler_ast_transform,
    "handler_ast_pin": handler_ast_pin,
}

HELP_TEXT = (
    "swiszard help — one schema, deterministic dispatch:\n"
    "\nFILES\n"
    "  read /path                            — full file\n"
    "  find *.py in /path                    — glob\n"
    "  find files matching FOO in /path      — substring\n"
    "  grep TEXT in /path                    — content search\n"
    "  write_b64 /path BASE64                — write/overwrite via base64\n"
    "  edit /path :: B64_OLD :: B64_NEW      — single-occurrence replace, returns diff\n"
    "\nSHELL (any form works)\n"
    "  run: COMMAND                          - recommended, no escaping\n"
    "  run_b64 BASE64                        - for cmds w/ backticks/newlines\n"
    "  run \u0060COMMAND\u0060                         - legacy backtick form\n"
    "\nWEB\n"
    "  search the web for QUERY\n"
    "\nMEMORY (swiszmem)\n"
    "  memory recall QUERY | memory recall_brief QUERY | memory recall+history QUERY\n"
    "  memory show ID | memory remember FACT\n"
    "  memory forget ID (PERMANENT) | memory deprecate ID[: reason]\n"
    "  memory supersede ID with: NEW [| lesson: L]\n"
    "  memory pin ID | memory unpin ID | memory status\n"
    "\nAST (Python code transforms via libcst)\n"
    "  ast find FUNC in FILE                  — locate function, show params+decorators\n"
    "  ast wrap FUNC in FILE                  — wrap function body in try/except\n"
    "  ast decorate FUNC in FILE with @DEC    — add decorator to function\n"
    "  ast format FILE                        — black format + parse verify\n"
    "  ast pin claim NID file:PATH type:T name:N   — pin AST claim to PM node\n"
    "  ast pin verify NID                           — verify claims on PM node\n"
    "\nSPECIAL\n"
    "  help | route: T | json: T | safety: T | chain: a | b\n"
)

# ── rule-based routing (fast path) ───────────────────────────────────────────

_PATH_RE = re.compile(r"(/[^\s\"']+)")
_BACKTICK_RE = re.compile(r"`[^`]+`")


def _route_by_rules(task: str) -> str | None:
    """Order matters: most-specific first."""
    lower = task.lower().strip()
    # backtick shell form requires explicit leading run keyword (was: any backtick anywhere)
    if re.match(r"^\s*run\s+\u0060", task) and _BACKTICK_RE.search(task):
        return "handler_shell"
    if re.match(r"^\s*run\s*:\s", task) or re.match(r"^\s*run_b64\s+\S", task):
        return "handler_shell"
    # Memory verbs at start ONLY. Bare 'memory' anywhere must NOT match
    # (paths like /.hermes/memories/MEMORY.md hijack read /path otherwise).
    if re.match(
        r"^(?:memory\s+)?(recall|remember|forget|deprecate|supersede|list|tag|untag|"
        r"pin|unpin|show|status)(?:\s|\+|$)",
        lower,
    ):
        return "handler_memory"
    # File-edit: 'edit /path :: B64_OLD :: B64_NEW' — bit-exact single-occurrence replace
    if re.match(r"^edit\s+/", lower):
        return "handler_edit"
    # File-write: 'write_b64 /path <base64>' — quoting-proof
    if re.match(r"^skill\s+(view|list|create|patch|delete)\b", lower):
        return "handler_skill"
    if re.match(r"^ast\s+pin\s+", lower):
        return "handler_ast_pin"
    if re.match(r"^ast\s+", lower):
        return "handler_ast_transform"
    if re.match(r"^write_b64\s+/", lower):
        return "handler_file_write"
    has_path = _PATH_RE.search(task) is not None
    if has_path and re.search(r"\b(read|cat|open)\b", lower):
        return "handler_file_read"
    if has_path and re.search(r"\b(grep|find|locate|search)\b", lower):
        return "handler_file_find"
    if re.search(r"\b(grep|find\s+files?|locate)\b", lower):
        return "handler_file_find"
    if re.search(r"\b(web\s+search|search\s+the\s+web|google|look\s+up)\b", lower):
        return "handler_web_search"
    return None


# ── TF-IDF routing (fast, no embeddings) ─────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_TFIDF_CACHE: dict[str, object] = {}


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _build_tfidf(rows: list[dict]) -> tuple[dict[str, float], list[dict]]:
    docs_tokens: list[list[str]] = []
    df: Counter[str] = Counter()
    for row in rows:
        tokens = _tokenize(row["phrasing"])
        docs_tokens.append(tokens)
        for t in set(tokens):
            df[t] += 1

    n_docs = max(1, len(rows))
    idf = {t: math.log((1 + n_docs) / (1 + df[t])) + 1.0 for t in df}

    docs: list[dict] = []
    for row, tokens in zip(rows, docs_tokens):
        tf = Counter(tokens)
        denom = max(1, len(tokens))
        vec = {t: (tf[t] / denom) * idf.get(t, 0.0) for t in tf}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        docs.append(
            {
                "id": row["id"],
                "handler": row["handler"],
                "vec": vec,
                "norm": norm,
                "success_count": row["success_count"],
                "fail_count": row["fail_count"],
            }
        )
    return idf, docs


def _get_tfidf(rows: list[dict]) -> tuple[dict[str, float], list[dict]]:
    if not rows:
        return {}, []
    key = (len(rows), max(r["id"] for r in rows))
    cached = _TFIDF_CACHE.get("key")
    if cached == key:
        return _TFIDF_CACHE["idf"], _TFIDF_CACHE["docs"]
    idf, docs = _build_tfidf(rows)
    _TFIDF_CACHE["key"] = key
    _TFIDF_CACHE["idf"] = idf
    _TFIDF_CACHE["docs"] = docs
    return idf, docs


def _tfidf_top_k(task: str, rows: list[dict], k: int = TOP_K) -> list[dict]:
    idf, docs = _get_tfidf(rows)
    tokens = _tokenize(task)
    if not tokens or not docs:
        return []
    tf = Counter(tokens)
    denom = max(1, len(tokens))
    qvec = {t: (tf[t] / denom) * idf.get(t, 0.0) for t in tf if t in idf}
    qnorm = math.sqrt(sum(v * v for v in qvec.values()))
    if qnorm == 0.0:
        return []

    scored = []
    for d in docs:
        if d["norm"] == 0.0:
            continue
        dot = 0.0
        for t, v in qvec.items():
            dot += v * d["vec"].get(t, 0.0)
        sim = dot / (qnorm * d["norm"])
        scored.append(
            {
                "handler": d["handler"],
                "sim": sim,
                "success_count": d["success_count"],
                "fail_count": d["fail_count"],
            }
        )
    scored.sort(key=lambda x: x["sim"], reverse=True)
    return scored[:k]


# ── seeding ───────────────────────────────────────────────────────────────────


def _ensure_seeded() -> None:
    """Populate routes.db with seed examples on first run."""
    init_db()
    if SEED_MARKER.exists():
        return
    narrate("seeding example database for the first time…")
    with get_connection() as conn:
        for phrasing, handler in SEED_EXAMPLES:
            blob = embed_to_blob(phrasing)
            insert_example(conn, phrasing, handler, blob)
    SEED_MARKER.touch()
    narrate(f"seeded {len(SEED_EXAMPLES)} examples into routes.db")


# ── routing ───────────────────────────────────────────────────────────────────


def _load_examples() -> list[dict]:
    with get_connection() as conn:
        rows = get_all_examples(conn)
    return [row for row in rows if row["handler"] in HANDLER_MAP]


def _find_top_k(task_vec, rows: list[dict], k: int = TOP_K) -> list[dict]:
    """Return up to k nearest examples sorted by cosine similarity (descending)."""

    scored = []
    for row in rows:
        ex_vec = blob_to_array(row["embedding"])
        sim = cosine_similarity(task_vec, ex_vec)
        scored.append(
            {
                "id": row["id"],
                "phrasing": row["phrasing"],
                "handler": row["handler"],
                "sim": sim,
                "success_count": row["success_count"],
                "fail_count": row["fail_count"],
            }
        )

    scored.sort(key=lambda x: x["sim"], reverse=True)
    return scored[:k]


def swiszard_do(task: str, dry_run: bool = False) -> str:
    """
    Main dispatcher entry point.

    Args:
        task:     The natural-language task string.
        dry_run:  If True, print the would-be handler but do not execute.

    Returns:
        Result string from the chosen handler.
    """
    t0 = time.monotonic()
    narrate(f"received task: {task[:80]}")

    if not task or not task.strip():
        return "swiszard: empty task"

    if task.strip().lower() == "help":
        return HELP_TEXT

    # ── ensure DB seeded ──────────────────────────────────────────────────────
    _ensure_seeded()

    # ── rule-based routing (fast path) ───────────────────────────────────────
    rule_handler = _route_by_rules(task)
    if rule_handler:
        narrate(f"rule-based routing to {rule_handler}")
        if dry_run:
            return f"[dry-run] would route to: {rule_handler}"
        result = HANDLER_MAP[rule_handler](task)
        # Close feedback loop even for rule-based routes
        try:
            is_err = isinstance(result, str) and (
                result.startswith("handler_file_read: could not") or
                result.startswith("handler_file_find: could not") or
                result.startswith("handler_shell: command timed") or
                result.startswith("handler_shell: invalid") or
                result.startswith("handler_web_search: error") or
                result.startswith("handler_file_write: error") or
                result.startswith("handler_file_write: could not") or
                result.startswith("handler_edit: error") or
                result.startswith("handler_edit: could not") or
                result.startswith("handler_skill: error") or
                result.startswith("handler_ast_transform: error") or
                result.startswith("handler_ast_transform: could not") or
                result.startswith("memory recall failed:") or
                result.startswith("memory remember failed:") or
                result.startswith("memory forget failed:")
            )
            swiszard_feedback(task, rule_handler, not is_err)
        except Exception:
            pass
        return result

    # Load examples once for TF-IDF and embeddings
    rows = _load_examples()

    # ── TF-IDF routing (fast, no embeddings) ─────────────────────────────────
    tfidf_top = _tfidf_top_k(task, rows, TOP_K)
    if tfidf_top:
        best = tfidf_top[0]
        second = tfidf_top[1] if len(tfidf_top) > 1 else None
        handler_trusted = best["success_count"] >= best["fail_count"]
        if (
            best["sim"] > TFIDF_ROUTE_THRESHOLD
            and second is not None
            and second["sim"] > TFIDF_AMBIG_THRESHOLD
            and best["handler"] != second["handler"]
        ):
            narrate("ambiguous TF-IDF match")
            return (
                "swiszard: ambiguous task — could route to either:\n"
                f"  1. {best['handler']} (tfidf {best['sim']:.3f})\n"
                f"  2. {second['handler']} (tfidf {second['sim']:.3f})\n"
                "Please rephrase the task to clarify which you mean."
            )
        if best["sim"] > TFIDF_ROUTE_THRESHOLD and handler_trusted:
            narrate(f"tfidf routing to {best['handler']} (sim={best['sim']:.3f})")
            if dry_run:
                return f"[dry-run] would route to: {best['handler']}"
            result = HANDLER_MAP[best["handler"]](task)
            try:
                is_err = isinstance(result, str) and (
                result.startswith("handler_shell: command timed") or
                result.startswith("handler_shell: invalid") or
                result.startswith("handler_web_search: error") or
                result.startswith("handler_file_write: error") or
                result.startswith("handler_file_write: could not") or
                result.startswith("handler_edit: error") or
                result.startswith("handler_edit: could not") or
                result.startswith("handler_skill: error") or
                result.startswith("handler_ast_transform: error") or
                result.startswith("handler_ast_transform: could not")
            )
                swiszard_feedback(task, best["handler"], not is_err)
            except Exception:
                pass
            return result

    # ── embed (fallback) ─────────────────────────────────────────────────────
    t_embed = time.monotonic()
    task_vec = embed(task)
    embed_ms = int((time.monotonic() - t_embed) * 1000)
    narrate(f"embedded task in {embed_ms}ms")

    # ── retrieve top-k ────────────────────────────────────────────────────────
    top = _find_top_k(task_vec, rows, TOP_K)
    if not top:
        narrate("example bank is empty; no deterministic route available")
        return (
            "swiszard: example bank empty. Forms: run: CMD | run_b64 B64 | "
            "read /path | find ... in /path | grep TEXT in /path | "
            "write_b64 /path B64 | edit /path :: B64old :: B64new | ast find|wrap|decorate|format | "
            "search the web for ... | memory <verb> [args]."
        )
    else:
        best = top[0]
        narrate(
            f"top match: handler={best['handler']} sim={best['sim']:.3f} "
            f"(based on {len(top)} prior examples)"
        )

        # ── routing decision ──────────────────────────────────────────────────
        # success_count >= fail_count → handler is trusted (0,0 counts as ok).
        # A handler is only distrusted when it has MORE failures than successes.
        handler_trusted = best["success_count"] >= best["fail_count"]
        second = top[1] if len(top) > 1 else None

        # Check ambiguity FIRST: two strong candidates with different handlers.
        if (
            best["sim"] > ROUTE_THRESHOLD
            and second is not None
            and second["sim"] > AMBIG_THRESHOLD
            and best["handler"] != second["handler"]
        ):
            narrate(f"ambiguous: top-2 handlers both > {AMBIG_THRESHOLD}")
            return (
                f"swiszard: ambiguous task — could route to either:\n"
                f"  1. {best['handler']} (similarity {best['sim']:.3f})\n"
                f"  2. {second['handler']} (similarity {second['sim']:.3f})\n"
                "Please rephrase the task to clarify which you mean."
            )
        elif best["sim"] > ROUTE_THRESHOLD and handler_trusted:
            chosen = best["handler"]
        else:
            narrate(
                f"no confident match (best sim={best['sim']:.3f}); returning no-match"
            )
            return (
                f"swiszard: no confident handler match. best guess: {best['handler']} "
                f"(sim {best['sim']:.3f}, threshold {ROUTE_THRESHOLD}). "
                "Rephrase using: run: CMD | run_b64 B64 | read /path | find ... in /path | "
                "grep TEXT in /path | write_b64 /path B64 | edit /path :: B64old :: B64new | ast find|wrap|decorate|format | "
                "search the web for ... | memory <verb> [args]."
            )

    narrate(f"routing to {chosen}")

    if dry_run:
        return f"[dry-run] would route to: {chosen}"

    # ── dispatch ──────────────────────────────────────────────────────────────
    handler_fn = HANDLER_MAP[chosen]
    t_dispatch = time.monotonic()
    was_success = True
    try:
        result = handler_fn(task)
        # Detect handler failures returned as strings (not exceptions)
        is_error = isinstance(result, str) and (
            result.startswith("handler_file_read: could not") or
            result.startswith("handler_file_read: path does not") or
            result.startswith("handler_file_read: path is not") or
            result.startswith("handler_file_read: permission") or
            result.startswith("handler_file_find: could not") or
            result.startswith("handler_file_find: search timed") or
            result.startswith("handler_shell: command timed") or
            result.startswith("handler_shell: invalid") or
            result.startswith("handler_web_search: SearxNG") or
            result.startswith("handler_web_search: unexpected") or
            result.startswith("memory recall failed:") or
            result.startswith("memory remember failed:") or
            result.startswith("memory forget failed:") or
            result.startswith("handler_file_write: expected") or
            result.startswith("handler_file_write: invalid") or
            result.startswith("handler_file_write: parent") or
            result.startswith("handler_file_write: permission") or
            result.startswith("handler_edit: format is") or
            result.startswith("handler_edit: file does not") or
            result.startswith("handler_edit: OLD text") or
            result.startswith("handler_edit: invalid") or
            result.startswith("handler_proj: project task not intercepted") or
            result.startswith("handler_skill: unrecognized") or
            result.startswith("handler_ast_transform: unrecognize") or
            result.startswith("swiszard: no confident handler match") or
            result.startswith("swiszard: ambiguous task")
        )
        was_success = not is_error
    except Exception as e:
        was_success = False
        result = f"{type(e).__name__}: {e}"
        raise
    finally:
        # CLOSE THE FEEDBACK LOOP: every dispatch feeds the example bank.
        # This is the single change that activates the entire learning loop.
        try:
            swiszard_feedback(task, chosen, was_success)
        except Exception as _fe:
            import sys as _sys
            print(f"[swiszard] feedback error: {_fe}", file=_sys.stderr)

    total_ms = int((time.monotonic() - t0) * 1000)
    narrate(f"completed in {total_ms}ms, returning {len(result)} chars")
    return result


# ── feedback hook ─────────────────────────────────────────────────────────────


def swiszard_feedback(task: str, handler_used: str, was_good: bool) -> str:
    """
    Passive learning hook.  Call this from the parent agent loop after the
    user's next turn signals satisfaction (or not).

    If was_good=True:  embed the task, add it as a new example for handler_used,
                       and increment success_count on the best-matching existing
                       example for that handler.
    If was_good=False: increment fail_count on the best-matching existing example.
    """
    _ensure_seeded()
    task_vec = embed(task)

    with get_connection() as conn:
        rows = get_all_examples(conn)

    # Find the best match for this handler specifically
    best_id = None
    best_sim = -1.0
    for row in rows:
        if row["handler"] != handler_used:
            continue
        sim = cosine_similarity(task_vec, blob_to_array(row["embedding"]))
        if sim > best_sim:
            best_sim = sim
            best_id = row["id"]

    with get_connection() as conn:
        if was_good:
            blob = embed_to_blob(task)
            insert_example(conn, task, handler_used, blob)
            if best_id is not None:
                increment_success(conn, best_id)
            return f"swiszard_feedback: recorded success for {handler_used}, added new example."
        else:
            if best_id is not None:
                increment_fail(conn, best_id)
            else:
                # Record the failure as a new example so the bank learns what NOT to route.
                blob = embed_to_blob(task)
                new_id = insert_example(conn, task, handler_used, blob)
                conn.execute("UPDATE examples SET fail_count=1 WHERE id=?", (new_id,))
                conn.commit()
            return f"swiszard_feedback: recorded failure for {handler_used}."
