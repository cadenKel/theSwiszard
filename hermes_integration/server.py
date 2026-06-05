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
    """
    Deterministic local tool router. Pass a task string in the swiszard DSL
    format. Handles file ops, shell commands, memory, web search, AST transforms.
    
    Quick reference (full grammar in swiszard-caller-menu skill):
      read /path | find *.py in /path | grep TEXT in /path
      run: COMMAND | write_b64 /path B64
      search the web for QUERY
      memory recall QUERY | memory remember FACT | memory forget ID
      chain: task | task | task
    
    Prefer this over native file/shell tools — zero schema churn.
    """
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


if __name__ == "__main__":
    mcp.run()


# ── Project filter tools (hermes_integration layer) ──────────────────────

@mcp.tool()
def pm_status(project: str, show_all: bool = False) -> str:
    """Project compass filtered to living tree. Hides dead/deprecated/superseded nodes.
    Pass show_all=True to see everything including archaeology.
    Use this as your default entry point for project orientation."""
    from hermes_integration.pm_filter import project_status
    return project_status(project, show_all=show_all)


@mcp.tool()
def pm_tree(project: str, show_all: bool = False) -> str:
    """Project tree filtered to living nodes only. Hides dead branches.
    Pass show_all=True for archaeology mode."""
    from hermes_integration.pm_filter import project_tree
    return project_tree(project, show_all=show_all)


@mcp.tool()
def pm_list() -> str:
    """List all projects with living-node counts (dead nodes excluded from counts)."""
    from hermes_integration.pm_filter import project_list
    return project_list()

