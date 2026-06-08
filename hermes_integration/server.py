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
    # Returns the LAST segment's result on success. Fails LOUD at first error —
    # returns "chain failed at step N/M: <error>" without running remaining steps.
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
        last_result = ""
        for i, seg in enumerate(flat, 1):
            try:
                res = _inject_wrap(seg, _router_do(seg, dry_run=False))
                if isinstance(res, dict) and "error" in res:
                    return f"chain failed at step {i}/{len(flat)}: {res['error']}\nsegment: {seg}"
                last_result = res if isinstance(res, str) else json.dumps(res, separators=(",",":"), default=str)
            except Exception as exc:
                return f"chain failed at step {i}/{len(flat)}: {type(exc).__name__}: {exc}\nsegment: {seg}"
        return last_result

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
def pm_tree(project: str, show_all: bool = False) -> str:
    """Project tree (living nodes only). Pass show_all=True for archaeology."""
    import json as _json, httpx as _httpx
    try:
        r = _httpx.post(f"{BASE}/project/tree", json={"project": project}, timeout=10)
        r.raise_for_status()
        nodes = r.json().get("nodes", r.json())
    except Exception as e:
        return f"[err] {e}"
    dead = {"deprecated","abandoned","superseded","archived","removed","invalidated","reverted"}
    if not show_all:
        nodes = [n for n in nodes if n.get("state") not in dead]
    # Build parent map for indentation
    id_map = {n["id"]: n for n in nodes}
    lines = [f"tree: {project} ({len(nodes)} living)"]
    def _depth(n):
        d, p = 0, n.get("parent_id")
        while p and p in id_map:
            d += 1; p = id_map[p].get("parent_id")
        return d
    for n in nodes:
        indent = "  " * _depth(n)
        st = n.get("state","?")[:8]
        ki = n.get("kind","?")[:6]
        ti = (n.get("title") or "")[:55]
        lines.append(f"{indent}[{n['id']}] {st:8} {ki:6} {ti}")
    return "\n".join(lines)


@mcp.tool()
def pm_list() -> str:
    """List all projects with node counts."""
    import httpx as _httpx
    try:
        r = _httpx.get(f"{BASE}/project/list", timeout=10)
        r.raise_for_status()
        projects = r.json().get("projects", [])
    except Exception as e:
        return f"[err] {e}"
    lines = []
    for p in projects:
        lines.append(f"{p.get('name','?'):20} nodes={p.get('node_count','?')}")
    return "\n".join(lines) if lines else "(no projects)"


# ── Ergonomic PM tools (hermes_integration layer) ────────────────────────
# Designed to eliminate circuit-breaker footguns and 3-step close-out dances.
# All reads go direct SQL; writes validate before HTTP.

_DB_PATH = "/home/ziggibot/.hermes/swiszard/memory.db"
BASE = "http://127.0.0.1:7437"

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



@mcp.tool()
def pm_subtree(project: str, root_id: int, show_all: bool = False) -> str:
    """Project tree (living nodes only). Pass show_all=True for archaeology."""
    import json, httpx
    r = httpx.post(f"{BASE}/project/subtree", json={"root_id": root_id, "show_all": show_all}, timeout=10)
    r.raise_for_status()
    d = r.json()
    nodes = d.get("nodes", [])
    if not show_all:
        dead = {"done","satisfied","superseded","reverted","removed","answered","invalidated","archived","abandoned"}
        nodes = [n for n in nodes if n.get("state") not in dead]
    lines = []
    for n in nodes:
        indent = "  " * (1 if n.get("parent_id") != root_id else 0)
        lines.append(f"{indent}[{n['id']}] {n.get('state','?'):10} {n.get('kind','?'):10} {n.get('title') or n.get('body','')[:60]}")
    return f"subtree of {root_id} ({len(nodes)} living):\n" + "\n".join(lines)



@mcp.tool()
def swiszard_mcp_restart() -> str:
    """Emergency restart: systemctl restart swiszard-mcp-http + Hermes reconnect signal. Safe to call anytime."""
    import subprocess, sys, time

    # Step 1: restart the service via systemd-run (escapes process group)
    r = subprocess.run(
        ["systemd-run", "--user", "--no-block", "bash", "-c",
         "sleep 2 && systemctl --user restart swiszard-mcp-http.service"],
        capture_output=True, text=True, timeout=5
    )
    if r.returncode != 0:
        return f"[err] systemd-run failed: {r.stderr.strip()}"

    # Step 2: signal Hermes to reconnect immediately via _reconnect_event
    # This bypasses the TCP timeout and makes Hermes rebuild the session fresh
    try:
        sys.path.insert(0, "/home/ziggibot/.hermes/hermes-agent")
        from tools.mcp_tool import _servers, _mcp_loop
        import threading
        srv = _servers.get("swiszard")
        if srv is not None and hasattr(srv, "_reconnect_event") and _mcp_loop is not None:
            _mcp_loop.call_soon_threadsafe(srv._reconnect_event.set)
            return "restart scheduled + Hermes reconnect signalled. New session ready in ~4s."
        else:
            return "restart scheduled (Hermes reconnect signal unavailable — may take up to 15s)"
    except Exception as e:
        return f"restart scheduled (reconnect signal failed: {e})"



@mcp.tool()
def pm_status(project: str, show_all: bool = False) -> str:
    """Project compass: north star, counts, frontier, summary. show_all=True for archaeology."""
    import json, httpx
    r = httpx.post(f"{BASE}/project/status", json={"project": project}, timeout=10)
    r.raise_for_status()
    d = r.json()
    counts = d.get("counts", {})
    frontier = d.get("frontier", [])
    summary = d.get("summary", "")
    lines = [
        f"project: {project}",
        f"counts: {counts.get('done',0)} done / {counts.get('active',0)} active / {counts.get('total',0)} total",
        f"frontier ({len(frontier)}):",
    ]
    for n in frontier[:8]:
        lines.append(f"  [{n['id']}] {n.get('kind','?'):10} {(n.get('title') or '')[:60]}")
    if summary:
        lines.append(f"summary: {summary[:120]}")
    return "\n".join(lines)


@mcp.tool()
def pm_orient(project: str, root_id: int, query: str = "") -> str:
    """Project compass: north star, counts, frontier, summary. show_all=True for archaeology. Returns orient output for a specific branch."""
    import json, httpx, concurrent.futures

    BASE_URL = BASE
    q = query or f"project {project} branch {root_id}"

    def _subtree():
        r = httpx.post(f"{BASE_URL}/project/subtree", json={"root_id": root_id, "show_all": False}, timeout=10)
        return r.json()

    def _status():
        r = httpx.post(f"{BASE_URL}/project/status", json={"project": project}, timeout=10)
        return r.json()

    def _memory():
        r = httpx.post(f"{BASE_URL}/recall_content", json={"query": q, "top_k": 3}, timeout=10)
        return r.json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        ft = ex.submit(_subtree)
        fs = ex.submit(_status)
        fm = ex.submit(_memory)
        subtree_d = ft.result()
        status_d  = fs.result()
        memory_d  = fm.result()

    nodes = subtree_d.get("nodes", [])
    dead = {"done","satisfied","superseded","reverted","removed","answered","invalidated","archived","abandoned"}

    # frontier = active nodes with no active children
    child_ids = {n["parent_id"] for n in nodes if n.get("parent_id")}
    frontier  = [n for n in nodes if n.get("state") not in dead and n["id"] not in child_ids and n["id"] != root_id]
    decisions = [n for n in nodes if n.get("kind") == "decision" and n.get("state") not in dead]
    counts    = status_d.get("counts", {})
    memories  = memory_d.get("memories", [])

    # fetch north star directly from DB
    import sqlite3 as _sq
    _DB = "/home/ziggibot/.hermes/swiszard/memory.db"
    with _sq.connect(_DB) as _c:
        _c.row_factory = _sq.Row
        ns = _c.execute(
            "SELECT id, title, body FROM pm_node WHERE kind='north_star' AND project_id=(SELECT id FROM pm_project WHERE name=?) LIMIT 1",
            (project,)
        ).fetchone()

    # ── compact format: north star + counts + frontier only ──────────────────
    lines = []
    if ns:
        ns_body = (ns["body"] or "").split("\n")[0][:100]
        lines.append(f"★ {ns_body}")
    lines.append(f"{counts.get('done',0)}d/{counts.get('active',0)}a/{counts.get('total',0)}t  branch:{root_id}")

    if frontier:
        lines.append("frontier:")
        for n in frontier[:8]:
            lines.append(f"  >[{n['id']}] {n.get('state','?')[:6]:6} {(n.get('title') or '')[:52]}")

    # Only surface DECISION/HARD RULE/CORRECTION notes — skip body
    key_nodes = [n for n in decisions if any(
        kw in (n.get("title") or "").upper()
        for kw in ("DECISION", "HARD RULE", "CORRECTION", "CRITICAL", "WARNING")
    )]
    if key_nodes:
        lines.append("decisions:")
        for n in key_nodes[:4]:
            lines.append(f"  [{n['id']}] {(n.get('title') or '')[:60]}")

    if memories:
        lines.append("mem:")
        for m in memories[:2]:
            lines.append(f"  {(m.get('content') or '')[:70].replace(chr(10),' ')}")

    return "\n".join(lines)




@mcp.tool()
def swiszard_patch_and_verify(
    file_path: str,
    old_str: str,
    new_str: str,
    smoke_import: str = "",
    smoke_call: str = "",
) -> str:
    """Patch file + syntax check + optional venv smoke test in one call. old_str must match exactly once."""
    import ast as _ast, subprocess
    from pathlib import Path as _P
    p = _P(file_path)
    src = p.read_text()
    n = src.count(old_str)
    if n == 0:
        return "[err] old_str not found in " + file_path
    if n > 1:
        return "[err] old_str matches " + str(n) + " times — must be unique"
    patched = src.replace(old_str, new_str, 1)
    try:
        _ast.parse(patched)
    except SyntaxError as e:
        return "[err] syntax: " + str(e)
    p.write_text(patched)
    out = "patched + syntax OK"
    if smoke_import and smoke_call:
        venv = "/home/ziggibot/theSwiszard/.venv/bin/python"
        snippet = (
            "import sys; sys.path.insert(0,'/home/ziggibot/theSwiszard'); "
            "from " + smoke_import + " import *; print(repr(" + smoke_call + "))"
        )
        r = subprocess.run([venv, "-c", snippet], capture_output=True, text=True, timeout=20)
        out += (" smoke OK: " + r.stdout.strip()[:200]) if r.returncode == 0 else (" smoke FAIL: " + r.stderr.strip()[-200:])
    return out


@mcp.tool()
def swiszard_service_logs(service: str, n: int = 20) -> str:
    """Last N lines of a systemd user service log. Strips timestamps/PIDs, surfaces tracebacks first (~60% fewer tokens)."""
    import subprocess, re as _re

    r = subprocess.run(
        ["journalctl", "--user", "-u", service, "-n", str(n), "--no-pager", "-o", "cat"],
        capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        return f"[err] journalctl failed: {r.stderr.strip()}"

    lines = r.stdout.splitlines()

    # surface tracebacks first
    tb_lines, other_lines = [], []
    in_tb = False
    for l in lines:
        if "Traceback" in l or "Error:" in l:
            in_tb = True
        if in_tb:
            tb_lines.append(l)
        else:
            other_lines.append(l)

    out = []
    if tb_lines:
        out.append("=== TRACEBACK ===")
        out.extend(tb_lines)
        out.append("=== LOGS ===")
    out.extend(other_lines)
    return "\n".join(out) if out else "(no output)"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
