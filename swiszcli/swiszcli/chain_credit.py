"""
chain_credit.py — SwiszNet credit assignment for wizard-to-wizard routing.

When the user corrects swiszard output after a chain of wizard calls,
this module replays the trace (from trace.py TraceWriter) backward and
assigns credit via pairwise comparison.

Uses:
  - trace.py TraceWriter for trace data (SQLite, parent_id chaining)
  - proof_loop.py EMA pattern for weight updates
  - source_weights.py persistence style for edge weights

Cross-wizard edge weights (wizard_A->wizard_B) track which transitions
are reliable. Distinct from Learner/Router single-wizard prediction.

Edge weight store: ~/.swiszcli/edge_weights.json
  Format: {"wizard_A->wizard_B": 0.75, ...}
  EMA-tracked confidence. 0.5 = neutral, >0.7 = strong, <0.3 = anti-pattern.

Goal: mitigate cloud LLM use. Offload routing decisions to deterministic,
traceable mechanisms that learn over time on a single 12GB GPU.
90's AI — symbolic, auditable, local-first.

NO LLM. Pure cosine comparison + EMA.
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path
from typing import Optional

EDGE_FILE = pathlib.Path.home() / ".swiszcli" / "edge_weights.json"

# EMA settings (same pattern as proof_loop.py)
EMA_ALPHA = 0.20
W_MIN, W_MAX = 0.10, 1.00


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _load_edges() -> dict[str, float]:
    if EDGE_FILE.is_file():
        try:
            return json.loads(EDGE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_edges(d: dict[str, float]) -> None:
    EDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EDGE_FILE.write_text(json.dumps(d, sort_keys=True, indent=2))


def get_edge_weight(from_wizard: str, to_wizard: str) -> float:
    """Delegates to WeightEngine; falls back to legacy file if engine unseen."""
    from .weight_engine import get_engine
    eng = get_engine()
    w = eng.edge_weight(from_wizard, to_wizard)
    if w == eng.default_weight:
        # seed from legacy file on first access
        edges = _load_edges()
        legacy_key = f"{from_wizard}->{to_wizard}"
        if legacy_key in edges:
            eng.set(f"edge:{from_wizard}:{to_wizard}", edges[legacy_key])
            eng.save()
            return edges[legacy_key]
    return w


def record_edge_correct(from_wizard: str, to_wizard: str) -> float:
    from .weight_engine import get_engine
    eng = get_engine()
    current = eng.edge_weight(from_wizard, to_wizard)
    target = min(W_MAX, current + 0.25)
    new_w = eng.observe(f"edge:{from_wizard}:{to_wizard}", target)
    eng.save()
    return new_w


def record_edge_incorrect(from_wizard: str, to_wizard: str) -> float:
    from .weight_engine import get_engine
    eng = get_engine()
    current = eng.edge_weight(from_wizard, to_wizard)
    target = max(W_MIN, current - 0.25)
    new_w = eng.observe(f"edge:{from_wizard}:{to_wizard}", target)
    eng.save()
    return new_w


def should_auto_route(from_wizard: str, to_wizard: str, confidence: float) -> bool:
    w = get_edge_weight(from_wizard, to_wizard)
    return w >= 0.70 and confidence >= 0.85


def replay_trace_chain(
    trace_writer,
    root_trace_id: str,
    corrected_output: str,
    alternative_outputs: Optional[dict[str, dict[str, str]]] = None,
) -> list[dict]:
    """
    Replay a wizard trace chain backward, comparing at each step.

    Uses the REAL trace.py TraceWriter (SQLite, parent_id chaining).
    Walks from root_trace_id through all descendants via children().

    Args:
        trace_writer: trace.TraceWriter instance
        root_trace_id: the trace ID of the final wizard run (where user corrected)
        corrected_output: what the output should have been
        alternative_outputs: map of trace_id -> {alt_wizard: hypothetical_output}

    Returns:
        list of credit assignments with verdicts
    """
    # Gather the full chain: walk from root up via parent_id
    chain = []
    current_id = root_trace_id
    while current_id:
        row = trace_writer.get(current_id)
        if row is None:
            break
        chain.append(row)
        current_id = row.get("parent_id")

    chain.reverse()  # chronological order

    assignments = []
    prev_wizard = None

    for row in chain:
        trace_id = row["id"]
        wizard = row["wizard"]
        result_json = row.get("result_json", "{}")
        try:
            actual = json.loads(result_json) if result_json else ""
        except Exception:
            actual = str(result_json) if result_json else ""

        alts = alternative_outputs.get(trace_id, {}) if alternative_outputs else {}

        actual_str = json.dumps(actual) if isinstance(actual, dict) else str(actual)
        better = None
        best_sim = _output_similarity(actual_str, corrected_output)

        for alt_name, alt_output in alts.items():
            alt_str = json.dumps(alt_output) if isinstance(alt_output, dict) else str(alt_output)
            alt_sim = _output_similarity(alt_str, corrected_output)
            if alt_sim > best_sim + 0.05:
                best_sim = alt_sim
                better = alt_name

        assignment = {
            "trace_id": trace_id,
            "wizard": wizard,
            "chosen_sim": round(best_sim, 4),
            "better_alternative": better,
        }

        if better and prev_wizard:
            record_edge_incorrect(prev_wizard, wizard)
            record_edge_correct(prev_wizard, better)
            assignment["verdict"] = f"{prev_wizard}->{wizard}: WRONG, should be {better}"
            assignment["edge_update"] = f"{prev_wizard}->{wizard} down, {prev_wizard}->{better} up"
        elif prev_wizard and best_sim > 0.5:
            record_edge_correct(prev_wizard, wizard)
            assignment["verdict"] = f"{prev_wizard}->{wizard}: OK (sim {best_sim:.3f})"
            assignment["edge_update"] = f"{prev_wizard}->{wizard} reinforced"

        assignments.append(assignment)
        prev_wizard = wizard

    return assignments


def _output_similarity(text_a: str, text_b: str) -> float:
    if not text_a and not text_b:
        return 1.0
    if not text_a or not text_b:
        return 0.0
    import difflib
    return difflib.SequenceMatcher(None, text_a, text_b).ratio()


def format_assignment_report(assignments: list[dict]) -> str:
    if not assignments:
        return "SwiszNet: no routing decisions to analyze."

    lines = ["SwiszNet chain credit (trace replay):"]
    for a in assignments:
        wizard = a["wizard"]
        verdict = a.get("verdict", "no verdict")
        edge = a.get("edge_update", "")
        lines.append(f"  [{a['trace_id']}] {wizard}: {verdict}")
        if edge:
            lines.append(f"    edge: {edge}")
    return chr(10).join(lines)
