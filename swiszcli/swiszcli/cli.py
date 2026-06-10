"""
cli.py — swiszcli: thin local agent REPL.

Architecture:
  - Project detection from cwd (git repo name) or --project flag
  - pm_orient at session start (reactive, uses user's actual words as query)
  - Single model call per turn (no deliberation harness)
  - Parser just forwards tool calls to MCP
  - Everything goes through the PM: facts, decisions, tasks, lessons

Usage:
  swiszcli                    # auto-detect project from cwd
  swiszcli --project myproj   # explicit project
  swiszcli --model llama3:8b  # override model (default: llama3:8b)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ── project detection ─────────────────────────────────────────────────────────

def detect_project() -> str:
    """Detect project name from cwd. Falls back to directory name."""
    cwd = Path.cwd()
    # Check if we're in a git repo
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
            cwd=str(cwd),
        )
        if result.returncode == 0:
            return Path(result.stdout.strip()).name
    except Exception:
        pass
    # Fallback: use cwd name
    return cwd.name


# ── model call ─────────────────────────────────────────────────────────────────

def call_model(messages: list[dict], model: str, base_url: str = "http://127.0.0.1:11434") -> str:
    """Single model call via Ollama HTTP. Returns raw text."""
    import urllib.request
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    return data.get("message", {}).get("content", "")


# ── tool call parser ──────────────────────────────────────────────────────────

def parse_tool_call(text: str) -> dict | None:
    """Parse a tool call from model output. Supports:
      TOOL: pm_add project=... body=... kind=... why=...
      TOOL: pm_transition node_id=... state=... why=...
      TOOL: pm_complete node_id=... file_path=... func_name=... why=...
      TOOL: pm_kill node_id=... reason=...
      TOOL: pm_orient project=... query=...
      TOOL: pm_node node_id=...
      TOOL: pm_subtree root_id=...
      TOOL: pm_status project=...
      TOOL: pm_list
      TOOL: swiszard_do task=...
      TOOL: swiszard_patch_and_verify file_path=... old_str=... new_str=... node_id=... why=...
    """
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("TOOL:"):
            continue
        rest = line[5:].strip()
        parts = rest.split(None, 1)
        if not parts:
            continue
        tool_name = parts[0]
        args_str = parts[1] if len(parts) > 1 else ""
        # Parse key=value pairs
        args = {}
        for token in args_str.split():
            if "=" in token:
                k, v = token.split("=", 1)
                args[k] = v
        return {"tool": tool_name, "args": args}
    return None


def execute_tool_call(tool_call: dict) -> str:
    """Execute a tool call via swiszard_do or direct PM HTTP."""
    tool = tool_call["tool"]
    args = tool_call["args"]

    if tool == "swiszard_do":
        task = args.get("task", "")
        return _swiszard_do(task)

    # PM tools: use swiszard_do to route to PM
    if tool == "pm_list":
        return _swiszard_do("pm list")

    if tool == "pm_orient":
        project = args.get("project", "swiszard")
        query = args.get("query", "")
        return _swiszard_do(f"pm orient project={project} query={query}")

    if tool == "pm_node":
        node_id = args.get("node_id", "0")
        return _swiszard_do(f"pm node {node_id}")

    if tool == "pm_subtree":
        root_id = args.get("root_id", "0")
        return _swiszard_do(f"pm subtree root_id={root_id}")

    if tool == "pm_status":
        project = args.get("project", "swiszard")
        return _swiszard_do(f"pm status project={project}")

    if tool == "pm_add":
        project = args.get("project", "swiszard")
        body = args.get("body", "")
        kind = args.get("kind", "task")
        state = args.get("state", "active")
        parent_id = args.get("parent_id", "0")
        title = args.get("title", "")
        why = args.get("why", "")
        return _swiszard_do(f"pm add project={project} body={body} kind={kind} state={state} parent_id={parent_id} title={title} why={why}")

    if tool == "pm_transition":
        node_id = args.get("node_id", "0")
        state = args.get("state", "")
        why = args.get("why", "")
        return _swiszard_do(f"pm transition node_id={node_id} state={state} why={why}")

    if tool == "pm_complete":
        node_id = args.get("node_id", "0")
        file_path = args.get("file_path", "")
        func_name = args.get("func_name", "")
        why = args.get("why", "")
        return _swiszard_do(f"pm complete node_id={node_id} file_path={file_path} func_name={func_name} why={why}")

    if tool == "pm_kill":
        node_id = args.get("node_id", "0")
        reason = args.get("reason", "")
        return _swiszard_do(f"pm kill node_id={node_id} reason={reason}")

    if tool == "swiszard_patch_and_verify":
        file_path = args.get("file_path", "")
        old_str = args.get("old_str", "")
        new_str = args.get("new_str", "")
        node_id = args.get("node_id", "0")
        why = args.get("why", "")
        return _swiszard_do(f"swiszard_patch_and_verify file_path={file_path} old_str={old_str} new_str={new_str} node_id={node_id} why={why}")

    return f"[err] unknown tool: {tool}"


def _swiszard_do(task: str) -> str:
    """Call swiszard_do via the swiszard HTTP server."""
    import urllib.request
    payload = json.dumps({"task": task}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:7437/swiszard_do",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.read().decode()
    except Exception as e:
        return f"[err] swiszard_do failed: {e}"


# ── system prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a local agent. Your project manager is your source of truth.

Rules:
1. At session start, call pm_orient to understand the project state.
2. Before any work, check pm_status or pm_subtree to see what's active.
3. Log ALL work to the PM: create task nodes before starting, transition to done when complete.
4. Use pm_add to create nodes (kind=task|objective|decision|question|artifact|note|lesson|north_star).
5. Use pm_transition to change node states (active|done|proposed|blocked|abandoned|satisfied|archived).
6. Use pm_complete to close tasks (requires file_path + func_name for AST pin verification).
7. Use pm_kill to abandon noise nodes.
8. Every mutating PM call MUST include why= (the reason for the audit log).
9. When a task completes, check for LESSON_REVIEW_PROMPT in the result and create lesson nodes for failures.
10. Use TOOL: prefix for all tool calls. One tool call per line.

Node kinds:
- north_star: one per project, "why does this project exist"
- objective: a goal or milestone
- task: a concrete unit of work
- decision: a choice made (with rationale)
- question: an open question
- artifact: a file, code, or output
- note: a fact or observation
- lesson: learned from failure (has trigger_text)

Available tools:
  TOOL: pm_orient project=<name> query=<text>
  TOOL: pm_status project=<name>
  TOOL: pm_subtree root_id=<id>
  TOOL: pm_node node_id=<id>
  TOOL: pm_list
  TOOL: pm_add project=<name> body=<text> kind=<kind> state=<state> parent_id=<id> title=<title> why=<reason>
  TOOL: pm_transition node_id=<id> state=<state> why=<reason>
  TOOL: pm_complete node_id=<id> file_path=<path> func_name=<name> why=<reason>
  TOOL: pm_kill node_id=<id> reason=<reason>
  TOOL: swiszard_patch_and_verify file_path=<path> old_str=<text> new_str=<text> node_id=<id> why=<reason>
  TOOL: swiszard_do task=<text>
"""


# ── REPL ───────────────────────────────────────────────────────────────────────

def run_repl(project: str, model: str, base_url: str):
    """Main REPL loop. Single model call per turn."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    print(f"swiszcli — project: {project} — model: {model}")
    print("Type 'quit' or Ctrl-D to exit.")
    print()

    # Session start: pm_orient
    orient_result = _swiszard_do(f"pm orient project={project}")
    print(f"[orient] {orient_result}")
    print()

    turn = 0
    while True:
        turn += 1
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("bye.")
            break

        messages.append({"role": "user", "content": user_input})

        # Single model call
        response = call_model(messages, model, base_url)
        messages.append({"role": "assistant", "content": response})

        print(f"agent> {response}")

        # Parse and execute tool calls
        tool_call = parse_tool_call(response)
        if tool_call:
            result = execute_tool_call(tool_call)
            print(f"  [{tool_call['tool']}] {result}")
            # Feed result back as user message
            messages.append({
                "role": "user",
                "content": f"Tool result: {result}",
            })

            # Check for lesson review prompt
            if "LESSON_REVIEW_PROMPT:" in result:
                lesson_text = result.split("LESSON_REVIEW_PROMPT:", 1)[1].strip()
                lesson_response = call_model(
                    messages + [{"role": "user", "content": lesson_text}],
                    model, base_url,
                )
                print(f"  [lesson] {lesson_response}")
                # Parse lesson tool calls
                lesson_tool = parse_tool_call(lesson_response)
                if lesson_tool:
                    lesson_result = execute_tool_call(lesson_tool)
                    print(f"  [{lesson_tool['tool']}] {lesson_result}")

        print()


# ── entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="swiszcli — thin local agent CLI")
    parser.add_argument("--project", "-p", default=None, help="Project name (default: auto-detect from cwd)")
    parser.add_argument("--model", "-m", default="llama3:8b", help="Ollama model (default: llama3:8b)")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434", help="Ollama base URL")
    args = parser.parse_args()

    project = args.project or detect_project()
    run_repl(project, args.model, args.base_url)


if __name__ == "__main__":
    main()
