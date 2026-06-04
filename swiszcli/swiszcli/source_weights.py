"""P1.8 source-weighting: trust-multiplier on retrieval scores.

Each memory has a `source` string (e.g. 'user', 'searxng:research_wizard',
'tool_output'). At recall time we multiply cosine similarity by a per-
source weight so high-trust memories outrank borderline-similar low-trust
ones. Pure data table + lookup, no LLM.

Override via ~/.swiszcli/source_weights.json — same keys override defaults.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_OVERRIDE_FILE = Path.home() / ".swiszcli" / "source_weights.json"

DEFAULTS = {
    "user":               1.00,
    "swiszcli":           0.85,   # historical default: bodies hand-written via CLI
    "mem-browser":        0.85,
    "tool_output":        0.90,
    "code_corpus":        0.85,
    "manual_doc":         0.95,
    "caden_self":         0.70,
    "inference":          0.65,
    "legacy":             0.50,
    # searxng tiers (handled via prefix match)
    "searxng:research_wizard": 0.55,  # auto-fetched, unfiltered
    "searxng:.gov":       0.85,
    "searxng:.edu":       0.80,
    "searxng:github.com": 0.65,
    "searxng:stackoverflow.com": 0.60,
    "searxng:_default":   0.45,
    # swiszard write paths
    "swiszard:memory remember": 0.85,
}

UNKNOWN_DEFAULT = 0.60


_LEARNED_FILE = Path.home() / ".swiszcli" / "source_weights_learned.json"


def _table() -> dict:
    """No cache: learned weights change per turn via proof_loop."""
    t = dict(DEFAULTS)
    # learned weights (proof_loop EMA) layer first
    if _LEARNED_FILE.is_file():
        try:
            data = json.loads(_LEARNED_FILE.read_text())
            if isinstance(data, dict):
                t.update({str(k): float(v) for k, v in data.items()})
        except Exception:
            pass
    # user explicit overrides take precedence over learned
    if _OVERRIDE_FILE.is_file():
        try:
            data = json.loads(_OVERRIDE_FILE.read_text())
            if isinstance(data, dict):
                t.update({str(k): float(v) for k, v in data.items()})
        except Exception:
            pass
    return t


def reset_cache():
    pass  # no longer cached; kept for back-compat


def weight_for(source) -> float:
    if not source:
        return UNKNOWN_DEFAULT
    s = str(source).strip()
    t = _table()
    if s in t:
        return t[s]
    # searxng:<domain> -> try _default and a couple suffix matches
    if s.startswith("searxng:"):
        domain = s[len("searxng:") :]
        for key in (f"searxng:{domain}", f"searxng:{domain.split("/")[0]}"):
            if key in t:
                return t[key]
        # tld suffix match
        for tld in (".gov", ".edu"):
            if domain.endswith(tld):
                return t.get(f"searxng:{tld}", t["searxng:_default"])
        return t.get("searxng:_default", UNKNOWN_DEFAULT)
    return UNKNOWN_DEFAULT


def apply_weights(rows, *, score_keys=("score", "trigger_score", "content_score")):
    """Multiply each row's score field(s) by weight_for(row['source']).

    Adds 'source_weight' and 'weighted_score' fields for transparency, then
    re-sorts by weighted_score descending. Returns new list, does not mutate.
    """
    if not rows:
        return rows
    out = []
    for r in rows:
        if not isinstance(r, dict):
            out.append(r)
            continue
        w = weight_for(r.get("source"))
        rr = dict(r)
        rr["source_weight"] = round(w, 3)
        base = 0.0
        for k in score_keys:
            if k in rr and isinstance(rr[k], (int, float)):
                base = rr[k]
                break
        rr["weighted_score"] = round(base * w, 4)
        out.append(rr)
    out.sort(key=lambda x: x.get("weighted_score", 0.0) if isinstance(x, dict) else 0.0, reverse=True)
    return out
