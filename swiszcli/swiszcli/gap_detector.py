"""P1.5 gap detector — flags LLM responses that need research before showing.

Sits at the LLM-output boundary. Scores draft responses on cheap regex
signals. Any flag means: hold response, fire research wizard, retry.

Sean: "model intelligence doesnt matter as long as it knows how to talk
and how to do research." The gap detector enforces that — model admits
ignorance fast, research wizard fills the gap.

NO LLM in this module. Pure regex + sqlite check (for "have we discussed").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Hedge phrases — model is signalling uncertainty
HEDGE_PATTERNS = [
    re.compile(r"\bi\s+think\b", re.I),
    re.compile(r"\bi\s+believe\b", re.I),
    re.compile(r"\bi'?m\s+not\s+(sure|certain)\b", re.I),
    re.compile(r"\bprobably\b", re.I),
    re.compile(r"\bmight\s+be\b", re.I),
    re.compile(r"\bas\s+of\s+(my\s+)?(training|knowledge|last\s+update)\b", re.I),
    re.compile(r"\bi\s+don'?t\s+(have|know)\b", re.I),
    re.compile(r"\bi\s+cannot\s+(verify|confirm)\b", re.I),
]

# Unsourced claims about external state — version strings, dates, prices
EXTERNAL_PATTERNS = [
    re.compile(r"\bversion\s+\d+\.\d+", re.I),
    re.compile(r"\blatest\s+(version|release)\b", re.I),
    re.compile(r"\bcurrent(ly)?\s+price\b", re.I),
    re.compile(r"\b(released|launched|published)\s+(in|on)\s+\d{4}", re.I),
    re.compile(r"\b(as\s+of)\s+\d{4}", re.I),
]

# Self-correction phrases — model is mid-fabrication
FABRICATION_PATTERNS = [
    re.compile(r"\bfor\s+example,?\s+let'?s\s+say\b", re.I),
    re.compile(r"\bhypothetical(ly)?\b", re.I),
    re.compile(r"\b(might\s+look\s+like|could\s+be\s+something\s+like)\b", re.I),
]


@dataclass
class GapVerdict:
    has_gap: bool
    hedge_hits: list = field(default_factory=list)
    external_hits: list = field(default_factory=list)
    fabrication_hits: list = field(default_factory=list)
    research_queries: list = field(default_factory=list)

    @property
    def summary(self):
        bits = []
        if self.hedge_hits:
            bits.append(f"{len(self.hedge_hits)} hedge")
        if self.external_hits:
            bits.append(f"{len(self.external_hits)} external")
        if self.fabrication_hits:
            bits.append(f"{len(self.fabrication_hits)} fabrication")
        return ", ".join(bits) if bits else "clean"


def detect(response_text):
    if not response_text or not response_text.strip():
        return GapVerdict(has_gap=False)
    hedge = [m.group(0) for p in HEDGE_PATTERNS for m in p.finditer(response_text)]
    ext = [m.group(0) for p in EXTERNAL_PATTERNS for m in p.finditer(response_text)]
    fab = [m.group(0) for p in FABRICATION_PATTERNS for m in p.finditer(response_text)]
    queries = []
    if hedge or fab:
        # Extract the sentence containing the hedge/fabrication as a query seed
        sentences = re.split(r"(?<=[.!?])\s+", response_text)
        for s in sentences:
            if any(p.search(s) for p in HEDGE_PATTERNS + FABRICATION_PATTERNS):
                cleaned = re.sub(r"\s+", " ", s).strip()
                if len(cleaned) > 10:
                    queries.append(cleaned[:200])
                if len(queries) >= 3:
                    break
    if ext:
        for hit in ext[:3]:
            queries.append(hit)
    return GapVerdict(
        has_gap=bool(hedge or ext or fab),
        hedge_hits=hedge,
        external_hits=ext,
        fabrication_hits=fab,
        research_queries=queries[:3],
    )


def hint_block(verdict):
    """Return a system-prompt block to inject into the model retry."""
    if not verdict.has_gap:
        return ""
    parts = ["<gap_detector>"]
    parts.append(f"  Your previous draft contained gaps: {verdict.summary}")
    if verdict.hedge_hits:
        parts.append(f"  Hedge phrases: {verdict.hedge_hits[:3]}")
    if verdict.external_hits:
        parts.append(f"  Unsourced external claims: {verdict.external_hits[:3]}")
    parts.append("  Research has been run on the questionable claims; see <research_context> below.")
    parts.append("  Rewrite your response using the research evidence. Do NOT hedge if evidence is clear.")
    parts.append("</gap_detector>")
    return "\n".join(parts)
