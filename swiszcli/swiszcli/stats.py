"""Lightweight stats collector for swisznet learning pipeline.

Each mechanism calls stats.incr('name') on successes/cache hits/etc.
Read via /stats slash command in swiszCLI.
Thread-safe. Persists to ~/.swiszcli/stats.json.
"""
from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from dataclasses import dataclass, field

STATS_FILE = Path.home() / ".swiszcli" / "stats.json"


@dataclass
class _Stats:
    # Speculative cache
    spec_hits: int = 0
    spec_misses: int = 0
    spec_attempts: int = 0

    # Edge weights
    edge_updates: int = 0
    edge_auto_routes: int = 0

    # Void detection
    voids_detected: int = 0
    voids_filled: int = 0

    # Sequence learning
    sequences_learned: int = 0
    sequence_hits: int = 0

    # Learner
    examples_learned: int = 0
    examples_reinforced: int = 0

    # Proof loop
    sources_used: int = 0
    sources_ignored: int = 0

    # Session
    session_started: float = field(default_factory=time.time)
    turns: int = 0


_lock = threading.Lock()
_stats = _Stats()


def _load():
    global _stats
    if STATS_FILE.exists():
        try:
            d = json.loads(STATS_FILE.read_text())
            for k, v in d.items():
                if hasattr(_stats, k):
                    setattr(_stats, k, type(getattr(_stats, k))(v))
        except Exception:
            pass


def _save():
    with _lock:
        d = {}
        for k, v in _stats.__dict__.items():
            if not k.startswith("_"):
                d[k] = v
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATS_FILE.write_text(json.dumps(d, indent=2))


def incr(field: str, amount: int = 1):
    with _lock:
        current = getattr(_stats, field, 0)
        setattr(_stats, field, current + amount)
    _save()


def snapshot() -> dict:
    with _lock:
        return {k: v for k, v in _stats.__dict__.items() if not k.startswith("_")}


def report() -> str:
    s = snapshot()
    turns = max(s["turns"], 1)
    spec_total = s["spec_hits"] + s["spec_misses"]
    spec_pct = (s["spec_hits"] / spec_total * 100) if spec_total > 0 else 0
    lines = [
        "swisznet stats:",
        "  turns: %d" % s["turns"],
        "  spec cache: %d/%d hits (%.0f%%)" % (s["spec_hits"], spec_total, spec_pct),
        "  edge weights: %d updates, %d auto-routes" % (s["edge_updates"], s["edge_auto_routes"]),
        "  voids: %d detected, %d filled" % (s["voids_detected"], s["voids_filled"]),
        "  sequences: %d learned, %d hints" % (s["sequences_learned"], s["sequence_hits"]),
        "  examples: %d new, %d reinforced" % (s["examples_learned"], s["examples_reinforced"]),
        "  proof loop: %d used, %d ignored" % (s["sources_used"], s["sources_ignored"]),
    ]
    return chr(10).join(lines)


# Load on import
_load()
