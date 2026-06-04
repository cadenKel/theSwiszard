"""narrate.py — opt-in stderr narration.
Silent by default (MCP context). Enable with SWISZARD_NARRATE=1 for dev.
"""
from __future__ import annotations
import os
import sys

_ENABLED = os.environ.get("SWISZARD_NARRATE", "").lower() in ("1", "true", "yes", "on")

def narrate(msg: str) -> None:
    if not _ENABLED:
        return
    print(f"[swiszard] {msg}", file=sys.stderr, flush=True)
