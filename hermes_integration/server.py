"""
Swiszard MCP server — deterministic router edition.

Exposes one MCP tool:
  swiszard_do(task)  — dispatcher + help + feedback

  Special task values:
    "help"             → handler format rules and usage contract
    "route: <task>"    → routing preview without execution (returns JSON)
    "feedback: <task> | <handler> | good|bad"  → record outcome

Routes tasks using CPU-only sentence-transformer embeddings + cosine similarity
against an example bank in SQLite.

Package lives at:
  /home/ziggibot/swiszard/swiszard/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the local swiszard package importable regardless of CWD.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.fastmcp import FastMCP
from swiszard.router import swiszard_do as _router_do
from swiszard.proactive_inject import wrap as _inject_wrap
from swiszard.router import swiszard_feedback as _router_feedback

mcp = FastMCP("swiszard")


@mcp.tool()
def swiszard_do(task: str) -> str:
    """Deterministic local dispatch: files, shell, memory, web, AST, PM, skills. Pass a task string. Full grammar in swiszard-caller-menu skill."""
    if not task or not task.strip():
        return "swiszard: empty task"

    # ── special task: help ───────────────────────────────────────────────
    if task.strip().lower() == "help":
        return _router_do("help", dry_run=True)

    # ── special prefix: route preview ─────────────────────────────────────
    if task.strip().lower().startswith("route:"):
        inner = task.strip()[len("route:"):].strip()
        if not inner:
            return "swiszard: usage: route: <task>"
        # dry-run reports the would-be handler without executing.
        return _router_do(inner, dry_run=True)

    # ── special prefix: chain ─────────────────────────────────────────────
    # Run multiple DSL segments serially. Separator is " | " or " then ".
    # Returns a JSON array of {segment, result} dicts. Fails LOUD: any
    # exception inside a segment is captured as {error: ...} for that
    # segment but does not abort the chain.
    if task.strip().lower().startswith("chain:"):
        import re as _re
        inner = task.strip()[len("chain:"):].strip()
        if not inner:
            return "swiszard: usage: chain: <task> | <task> | ..."
        segments = _re.split(r"\s+then\s+", inner, flags=_re.IGNORECASE)
        flat = []
        for seg in segments:
            flat.extend(p.strip() for p in seg.split("|"))
        flat = [s for s in flat if s]
        if not flat:
            return "swiszard: chain: no segments after split"
        results = []
        for seg in flat:
            try:
                res = _inject_wrap(seg, _router_do(seg, dry_run=False))
            except Exception as exc:
                res = {"error": f"{type(exc).__name__}: {exc}"}
            results.append({"segment": seg, "result": res})
        return json.dumps(results, separators=(",", ":"), default=str)

    # ── special prefix: feedback ──────────────────────────────────────────
    if task.strip().lower().startswith("feedback:"):
        inner = task.strip()[len("feedback:"):].strip()
        parts = [p.strip() for p in inner.split("|")]
        if len(parts) != 3:
            return (
                "swiszard: usage: feedback: <original task> | <handler_used> | good|bad\n"
                "Example: feedback: run `df -h` | handler_shell | good"
            )
        orig_task, handler_used, verdict = parts
        was_good = verdict.lower() in ("good", "yes", "true", "1", "correct")
        return _router_feedback(orig_task, handler_used, was_good)

    return _inject_wrap(task, _router_do(task, dry_run=False))


def _wrap_result(raw: str) -> str:
    """Compress agent output for Hermes: [ok]/[err] prefix + 2000-char cap."""
    if not raw:
        return "[ok] (empty output)"
    raw = raw.strip()
    is_err = raw.startswith("[err]") or "error" in raw[:40].lower() or "traceback" in raw[:80].lower()
    prefix = "[err] " if is_err else "[ok] "
    body = raw[len("[err] "):].strip() if raw.startswith("[err]") else raw
    body = body[len("[ok] "):].strip() if body.startswith("[ok]") else body
    if len(body) > 2000:
        body = body[:1960] + "...(truncated)"
    return prefix + body


# ── Tier-2 agent tool ────────────────────────────────────────────────────

@mcp.tool()
def swiszard_agent_do(spec: str, timeout: int = 300) -> str:
    """Full swiszard agent: wizard routing + LLM membrane. Pass natural-language spec. Local machine executes. Slower than swiszard_do — use only when deterministic handlers don't cover the task."""
    if not spec or not spec.strip():
        return "[err] swiszard_agent_do: empty spec"
    try:
        from swiszcli.headless import run_headless
        raw = run_headless(spec, timeout=timeout)
    except ImportError as e:
        return f"[err] headless not available: {e}"
    except Exception as e:
        return f"[err] {type(e).__name__}: {e}"
    return _wrap_result(raw)



# ── PM write tools ───────────────────────────────────────────────────────

@mcp.tool()
def pm_add(project: str, body: str, kind: str = "task", state: str = "active", parent_id: int = 0, title: str = "") -> str:
    """Add a node. kind=task|objective|decision|question|artifact|note. state=active|proposed|done."""
    import httpx, json
    payload = {"project": project, "body": body, "kind": kind, "state": state}
    if parent_id:
        payload["parent_id"] = parent_id
    if title:
        payload["title"] = title
    r = httpx.post("http://127.0.0.1:7437/project/add_node", json=payload, timeout=10)
    r.raise_for_status()
    d = r.json()
    return f'added node #{d["node_id"]} to {project}'


@mcp.tool()
def pm_transition(node_id: int, state: str) -> str:
    """Change a project node state. States: active, done, proposed, blocked, abandoned, satisfied, archived."""
    import httpx, json
    r = httpx.post("http://127.0.0.1:7437/project/transition", json={"node_id": node_id, "state": state}, timeout=10)
    r.raise_for_status()
    d = r.json()
    return f'node {node_id}: {d.get("old_state","?")} -> {d.get("new_state",state)}'



# ── Project filter tools (hermes_integration layer) ──────────────────────

@mcp.tool()
def pm_status(project: str, show_all: bool = False) -> str:
    """Project compass: north star, counts, frontier, summary. show_all=True for archaeology."""
    from hermes_integration.pm_filter import project_status
    return project_status(project, show_all=show_all)


@mcp.tool()
def pm_tree(project: str, show_all: bool = False) -> str:
    """Project tree (living nodes only). Pass show_all=True for archaeology."""
    from hermes_integration.pm_filter import project_tree
    return project_tree(project, show_all=show_all)


@mcp.tool()
def pm_list() -> str:
    """List all projects with node counts."""
    from hermes_integration.pm_filter import project_list
    return project_list()


# ── Ergonomic PM tools (hermes_integration layer) ────────────────────────
# Designed to eliminate circuit-breaker footguns and 3-step close-out dances.
# All reads go direct SQL; writes validate before HTTP.

_DB_PATH = "/home/ziggibot/.hermes/swiszard/memory.db"

_VALID_TRANSITIONS = {
    "proposed":   {"active", "abandoned"},
    "active":     {"satisfied", "abandoned", "done", "blocked"},
    "blocked":    {"active", "abandoned"},
    "abandoned":  {"active"},
    "done":       set(),
    "satisfied":  set(),
    "committed":  {"superseded", "reverted"},
    "superseded": set(),
    "reverted":   set(),
    "deprecated": {"removed"},
    "removed":    set(),
    "open":       {"researching", "answered", "invalidated", "parked"},
    "researching":{"answered", "invalidated", "parked"},
    "parked":     {"open", "researching"},
    "answered":   set(),
    "invalidated":set(),
    "archived":   set(),
}

def _db_node(node_id: int):
    import sqlite3
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    return c.execute(
        "SELECT id, kind, state, title, body, tags FROM pm_node WHERE id=?", (node_id,)
    ).fetchone()


@mcp.tool()
def pm_node(node_id: int) -> str:
    """Inspect a single PM node: state, kind, body, pin status, valid next transitions. Zero HTTP — pure SQL read."""
    import json
    row = _db_node(node_id)
    if not row:
        return json.dumps({"error": f"node {node_id} not found"})
    try:
        tags = json.loads(row["tags"]) if row["tags"] else []
    except Exception:
        tags = []
    valid_next = sorted(_VALID_TRANSITIONS.get(row["state"], set()))
    has_claim = any("ast_claim" in str(t) for t in tags)
    is_stale  = "stale_pin" in tags
    return json.dumps({
        "id": row["id"],
        "kind": row["kind"],
        "state": row["state"],
        "title": row["title"],
        "body": (row["body"] or "")[:200],
        "valid_next": valid_next,
        "pin": {"claimed": has_claim, "stale": is_stale},
    }, separators=(",", ":"))


@mcp.tool()
def pm_safe_transition(node_id: int, state: str) -> str:
    """Transition a PM node state. Pre-validates via SQL — never fires a 400. Returns error + valid_next on bad transitions."""
    import json, httpx
    row = _db_node(node_id)
    if not row:
        return json.dumps({"error": f"node {node_id} not found"})
    valid = _VALID_TRANSITIONS.get(row["state"], set())
    if not valid:
        return json.dumps({"error": f"node {node_id} is terminal ({row['state']}) — cannot transition"})
    if state not in valid:
        return json.dumps({
            "error": f"invalid transition {row['state']} -> {state}",
            "valid_next": sorted(valid),
        })
    r = httpx.post(f"{BASE}/project/transition", json={"node_id": node_id, "state": state}, timeout=10)
    r.raise_for_status()
    d = r.json()
    return f"node {node_id}: {d.get('old_state','?')} -> {d.get('new_state', state)}"


@mcp.tool()
def pm_complete(node_id: int, file_path: str, func_name: str) -> str:
    """Atomic task close-out: ast pin claim + verify + transition to done. Fails loud at any step."""
    import json
    from swiszard.router import swiszard_do as _do
    # 1. pin claim
    claim_result = _do(f"ast pin claim {node_id} file:{file_path} type:Function name:{func_name}", dry_run=False)
    try:
        claim_d = json.loads(claim_result)
    except Exception:
        return f"[err] pin claim parse failed: {claim_result[:200]}"
    if not claim_d.get("ok"):
        return f"[err] pin claim failed: {claim_result[:200]}"
    # 2. verify
    verify_result = _do(f"ast pin verify {node_id}", dry_run=False)
    try:
        verify_d = json.loads(verify_result)
    except Exception:
        return f"[err] pin verify parse failed: {verify_result[:200]}"
    if not verify_d.get("verified"):
        return f"[err] pin not verified: {verify_result[:300]}"
    # 3. transition
    transition_result = pm_safe_transition(node_id, "done")
    lineno = ""
    for claim in verify_d.get("claims", []):
        ev = claim.get("evidence", {}).get("found_at", {})
        if ev.get("lineno"):
            lineno = f":{ev['lineno']}"
            break
    return f"node {node_id} done. pin verified at {file_path}{lineno}"


@mcp.tool()
def pm_kill(node_id: int, reason: str = "") -> str:
    """Force-terminate any PM node via SQL regardless of state machine. For noise nodes and dead weight."""
    import sqlite3, time as _time, json
    row = _db_node(node_id)
    if not row:
        return json.dumps({"error": f"node {node_id} not found"})
    terminal = {"done", "satisfied", "superseded", "reverted", "removed", "answered", "invalidated", "archived"}
    if row["state"] in terminal:
        return f"node {node_id} already terminal ({row['state']}), nothing to do"
    # pick landing state by kind
    decision_kinds = {"decision"}
    artifact_kinds = {"artifact"}
    if row["kind"] in decision_kinds:
        target = "superseded"
    elif row["kind"] in artifact_kinds:
        target = "abandoned"
    else:
        target = "abandoned"
    c = sqlite3.connect(_DB_PATH)
    c.execute("UPDATE pm_node SET state=?, updated=? WHERE id=?", (target, int(_time.time()), node_id))
    c.commit()
    suffix = f" | reason: {reason}" if reason else ""
    return f"killed node {node_id} ({row['kind']}:{row['state']} -> {target}): {(row['title'] or '')[:60]}{suffix}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
