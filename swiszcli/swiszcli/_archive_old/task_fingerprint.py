"""P1.15 task fingerprint: blend what user SAID with what user is DOING.

Pure vector math, no LLM. Builds a short text fingerprint each turn from:
  - project name (if any)
  - last N tool/swiszard tasks issued
  - last edited file paths (if available)

Embeds it, then blends with the user-query vector at BLEND_ALPHA. The
blended vector is what we hand to recall_chunks. Effect: when you're
mid-edit on auth.py and ask "why does this fail", recall surfaces auth-
related context, not generic "why does X fail" matches.
"""
from __future__ import annotations

from collections import deque

# how much of the final vector comes from the fingerprint vs the user query
BLEND_ALPHA = 0.30
TASK_BUFFER_SIZE = 6


class TaskFingerprint:
    def __init__(self, buffer_size=TASK_BUFFER_SIZE):
        self.tasks = deque(maxlen=buffer_size)
        self.files = deque(maxlen=buffer_size)
        self.project = ""

    def set_project(self, name):
        self.project = (name or "")[:80]

    def record_task(self, task_str):
        t = (task_str or "").strip()
        if not t:
            return
        # extract any paths from the task (cheap heuristic)
        for tok in t.split():
            if "/" in tok and len(tok) < 200:
                self.files.append(tok.strip('"\'`,;)('))
        self.tasks.append(t[:200])

    def record_file(self, path):
        p = (path or "").strip()
        if p:
            self.files.append(p[:200])

    def render(self):
        parts = []
        if self.project:
            parts.append(f"project: {self.project}")
        if self.files:
            parts.append("recent files: " + ", ".join(list(self.files)[-3:]))
        if self.tasks:
            parts.append("recent actions: " + " | ".join(list(self.tasks)[-3:]))
        return "\n".join(parts)

    def is_empty(self):
        return not (self.project or self.files or self.tasks)


def blend(query_vec, fingerprint_vec, alpha=BLEND_ALPHA):
    """Convex combination: (1-alpha)*query + alpha*fingerprint."""
    if not query_vec:
        return fingerprint_vec
    if not fingerprint_vec or len(query_vec) != len(fingerprint_vec):
        return query_vec
    a = float(alpha)
    return [(1.0 - a) * q + a * f for q, f in zip(query_vec, fingerprint_vec)]
