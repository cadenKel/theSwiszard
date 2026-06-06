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
        segments = _re.split(r"\\s+then\\s+", inner, flags=_re.IGNORECASE)
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


if __name__ == "__main__":
    mcp.run()


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

