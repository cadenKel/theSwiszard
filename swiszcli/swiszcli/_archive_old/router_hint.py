"""Router-hint injection for the agent loop.

P0 wiring strategy: the router does NOT execute. It produces a hint
that gets appended to the system prompt for the current turn. The LLM
still emits <<SWISZ>> calls; it just has a strong nudge.

This is the conservative wiring path. It buys us:
  - router observability (we see what the router would have picked)
  - learning signal for P1 (every model swiszard call can be compared
    to the routers prediction)
  - no risk of breaking existing CLI behavior

P1 will add deterministic arg extractors for the SILENT mode and
actually bypass the LLM. P0 just injects hints.

Fails LOUDLY on embed errors (no silent fallback).
"""
from __future__ import annotations

from .router import Router, Decision


def router_hint(decision: Decision) -> str:
    if decision.mode == "fallback":
        return ""
    confidence = {
        "silent": "HIGH",
        "preview": "MEDIUM",
        "prompt": "LOW",
    }.get(decision.mode, "NONE")
    return (
        f"<router_hint confidence={confidence}>" + chr(10)
        + f"  Best matching wizard: {decision.wizard_name}" + chr(10)
        + f"  Matched on seed phrase: {decision.matched_text!r}" + chr(10)
        + f"  Cosine score: {decision.score:.3f}" + chr(10)
        + f"  Notes: This is a HINT from the deterministic router based on user phrasing." + chr(10)
        + f"  If this matches the user intent, emit a swiszard task that uses the {decision.wizard_name} wizard pattern." + chr(10)
        + f"  If it does NOT match, ignore this hint." + chr(10)
        + f"</router_hint>"
    )


def compose_extra_system(router: Router, user_text: str, chunk_hint: str = "") -> str:
    """Compose the full extra_system block: router hint + recalled chunks."""
    try:
        decision = router.decide(user_text)
    except Exception as e:
        # router failure is loud but non-fatal
        return f"<router-error>{e}</router-error>" + (chr(10) + chunk_hint if chunk_hint else "")
    rh = router_hint(decision)
    parts = [p for p in (rh, chunk_hint) if p]
    return chr(10).join(parts)
