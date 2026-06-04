"""P1.12 proof loop: did the model USE injected evidence?

When research_wizard injects evidence, we stash (source, embedding) pairs.
On the NEXT draft, we measure cosine(draft_embedding, evidence_embedding).
  - high overlap (>=0.55) -> source was USED -> EMA boost its weight
  - low overlap  (<0.30)  -> source was IGNORED -> EMA decay

Learned weights are persisted to ~/.swiszcli/source_weights_learned.json
and layered into source_weights._table() between defaults and explicit
user overrides. No LLM.
"""
from __future__ import annotations

import json
from pathlib import Path

LEARNED_FILE = Path.home() / ".swiszcli" / "source_weights_learned.json"

USED_THRESHOLD     = 0.55
IGNORED_THRESHOLD  = 0.30
EMA_ALPHA          = 0.20    # how fast learned weight moves per signal
W_MIN, W_MAX       = 0.10, 1.00


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _load_learned():
    if LEARNED_FILE.is_file():
        try:
            return json.loads(LEARNED_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_learned(d):
    LEARNED_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_FILE.write_text(json.dumps(d, sort_keys=True, indent=2))


class ProofLoop:
    """Hold pending evidence; on next draft, score & update weights."""

    def __init__(self):
        # list of (source, evidence_vector, base_default_weight)
        self.pending = []

    def stash(self, source, evidence_vec, base_weight=None):
        if not source or not evidence_vec:
            return
        self.pending.append((str(source), list(evidence_vec), base_weight))

    def has_pending(self):
        return bool(self.pending)

    def score_against(self, draft_vec):
        """Return list of (source, similarity, verdict) for caller logging."""
        results = []
        if not self.pending or not draft_vec:
            self.pending.clear()
            return results
        learned = _load_learned()
        # group by source: take MAX similarity per source
        by_source = {}
        for src, ev, base in self.pending:
            sim = _cosine(draft_vec, ev)
            cur = by_source.get(src, (0.0, base))
            if sim > cur[0]:
                by_source[src] = (sim, base)
        for src, (sim, base) in by_source.items():
            current = float(learned.get(src, base if base is not None else 0.6))
            if sim >= USED_THRESHOLD:
                target = min(W_MAX, current + 0.20)
                verdict = "used"
            elif sim <= IGNORED_THRESHOLD:
                target = max(W_MIN, current - 0.20)
                verdict = "ignored"
            else:
                target = current
                verdict = "ambiguous"
            new_w = current + EMA_ALPHA * (target - current)
            new_w = max(W_MIN, min(W_MAX, new_w))
            learned[src] = round(new_w, 4)
            results.append((src, round(sim, 3), verdict, round(new_w, 3)))
        _save_learned(learned)
        self.pending.clear()
        return results
