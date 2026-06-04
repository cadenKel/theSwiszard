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
_REPO_ROOT = Path(__file__).resolve().parent
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
    Swiszard: one-tool deterministic router. Pass a task string in the DSL
    below; the router regex-dispatches to a handler. No LLM, no embeddings
    at call time — pure pattern match. PREFER THIS over native file/shell
    tools when the operation fits the DSL: same result, zero schema churn,
    one tool slot instead of many.

    ── DSL GRAMMAR ──────────────────────────────────────────────────────────
    FILES
      read /abs/path                              full file
      find *.py in /abs/path                      glob match
      find files matching FOO in /abs/path        substring match
      grep TEXT in /abs/path                      content search
      write_b64 /abs/path BASE64                  write/overwrite (base64-encode
                                                  arbitrary content to avoid
                                                  quote/newline/backtick hell)

    SHELL
      run `COMMAND`                               command MUST be in backticks

    WEB
      search the web for QUERY                    local SearxNG

    MEMORY (swiszmem)
      memory recall QUERY                         semantic search
      memory recall+history QUERY                 includes deprecated entries
      memory show ID                              full entry by id
      memory remember FACT                        write new fact
      memory forget ID                            PERMANENT delete
      memory deprecate ID[: reason]               soft-delete with reason
      memory supersede ID with: NEW [| lesson: L] replace + optional lesson
      memory pin ID  |  memory unpin ID           protect from pruning
      memory list [--tag T --source S]            browse
      memory tag ID T  |  memory untag ID T       label management
      memory status                               counts + health

    ── AST TRANSFORMS (Python code, deterministic via libcst) ────────────
      ast find FUNC in FILE                  — locate function, show params+decorators
      ast wrap FUNC in FILE                  — wrap body in try/except Exception
      ast decorate FUNC in FILE with @DEC    — add decorator to function
      ast format FILE                        — black format + parse verify
    ── SPECIAL PREFIXES ─────────────────────────────────────────────────────
      help                       Print full handler contract.
      route: <task>              Dry-run: show which handler would fire (JSON).
      json: <task>               Execute and wrap in {handler, stdout, stderr,
                                 exit_code, duration_ms}.
      chain: a | b | c           Run segments serially (or "a then b then c").
                                 Returns [{segment, result}, ...].
      safety: <task>             Preview command + destructive/safe verdict
                                 BEFORE running. Use for rm/dd/mkfs/redirects.
      feedback: <task> | <handler> | good|bad
                                 Record outcome — trains the router.

    ── WHEN TO USE THIS vs NATIVE TOOLS ─────────────────────────────────────
    USE swiszard_do for: file read/find/grep, shell commands, memory ops, web
    search, chained operations, and anything where you'd otherwise call 3+
    native tools in sequence (use `chain:`).

    USE native tools (write_file, patch, read_file with offset/limit) when:
    you need partial reads with line numbers, surgical patches via fuzzy
    match, or structured args that the DSL can't express cleanly.

    Args:
        task: A single self-contained instruction in the DSL above, or a
              special prefix.

    Returns:
        Handler result, help text, routing JSON, or feedback confirmation.
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
