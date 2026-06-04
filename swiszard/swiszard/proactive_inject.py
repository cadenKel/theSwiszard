"""
proactive_inject.py — Prepend trigger-matched memories to swiszard_do output.

Design: when a tool call comes in, embed the task, match against trigger
embeddings, prepend the top non-pinned non-noisy hits to the handler output.
This is the LibbieAI moat (memory:703) running in the tool-call return path,
host-agnostic.

Quiet-fail on purpose: any error in injection MUST NOT break the handler call.
The handler succeeded; injection is gravy.
"""
from __future__ import annotations
import json
import re
import urllib.request

MEMORY_SERVER = "http://127.0.0.1:7437"
TOP_K = 5
MIN_SCORE = 0.75
MAX_INJECT = 3
PREVIEW_CHARS = 200

# Skip injection entirely for these task shapes — would be redundant or noisy.
_SKIP_PREFIXES = ("memory ", "route:", "feedback:", "help", "json:", "safety:")
_SKIP_EXACT = ("help", "status")


def _should_skip(task: str) -> bool:
    t = task.strip().lower()
    if not t:
        return True
    if t in _SKIP_EXACT:
        return True
    for p in _SKIP_PREFIXES:
        if t.startswith(p):
            return True
    return False


def _recall(query: str) -> list[dict]:
    try:
        payload = json.dumps({"query": query, "top_k": TOP_K}).encode()
        req = urllib.request.Request(
            MEMORY_SERVER + "/recall_triggers",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        return data.get("memories", []) or []
    except Exception:
        return []


def _format(mems: list[dict]) -> str:
    keep = []
    for m in mems:
        # Skip always_inject pinned — already in the prompt, don't double-bill.
        if m.get("pinned"):
            continue
        if m.get("matched_trigger") == "<always_inject>":
            continue
        score = m.get("trigger_score", 0)
        if score < MIN_SCORE:
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        # Filter obvious noise: compaction logs, raw user prompts repeated back
        if content.startswith(("⟳", "🗜️", "Compacting")):
            continue
        if len(content) > PREVIEW_CHARS:
            content = content[:PREVIEW_CHARS].rstrip() + f"... [+{len(m['content']) - PREVIEW_CHARS}ch; memory show {m['id']}]"
        keep.append(f"  [memory:{m['id']} s={score:.2f}] {content}")
        if len(keep) >= MAX_INJECT:
            break
    if not keep:
        return ""
    return "<swizmem-proactive>\n" + "\n".join(keep) + "\n</swizmem-proactive>\n\n"


def wrap(task: str, handler_output: str) -> str:
    """Prepend matched memories to handler_output. Quiet-fail on any error."""
    try:
        if _should_skip(task):
            return handler_output
        mems = _recall(task)
        prefix = _format(mems)
        if not prefix:
            return handler_output
        return prefix + handler_output
    except Exception:
        return handler_output
