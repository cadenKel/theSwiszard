"""swiszRouter: examples-table router for swiszCLI P0.

Single mechanism: cosine-match user input against the examples table.
Threshold ladder decides act/preview/prompt/LLM.

The router does NOT execute. It returns a Decision the caller (agent.py)
acts on. Keeps this module side-effect free except for win/loss writes.

Wizard names returned are pure strings — the caller is responsible for
invoking the wizard, which then INTERVIEWS for its own args (per the
wizard-as-interviewer discipline in the MVP design doc).

Fails LOUDLY. No silent fallbacks.
"""
from __future__ import annotations

from dataclasses import dataclass

from .context_store import ContextStore
from .embed import embed

# ---------------------------------------------------------------------------
# HANDLER SEEDS — 3-5 trigger phrases per wizard.
# Add a wizard? Add an entry here. embed at startup, written to examples
# table with source=seed, weight=1.0.
# ---------------------------------------------------------------------------
HANDLER_SEEDS: dict[str, list[str]] = {
    "read": [
        "read the file at",
        "show me the contents of",
        "open this file",
        "what does this file say",
        "cat this file",
    ],
    "grep": [
        "search for in",
        "find lines containing",
        "grep for in",
        "where is this string in",
        "find all occurrences of",
    ],
    "find_files": [
        "find files matching",
        "list files like",
        "where are the python files in",
        "find all the .md files",
        "look for files named",
    ],
    "shell": [
        "run this command",
        "execute in the shell",
        "run a bash command",
        "run this on the terminal",
        "shell out and",
    ],
    "research": [
        "look this up on the web",
        "what is the latest on",
        "search the internet for",
        "find current information about",
        "research this topic",
    ],
    "remember": [
        "save this to memory",
        "remember that",
        "add this to swizmem",
        "store this fact",
        "make a note of this",
    ],
    "recall": [
        "what did we say about",
        "remind me about",
        "what do you remember about",
        "search memory for",
        "recall everything about",
    ],
}

# Threshold ladder. Compared against the RAW cosine score (not weighted).
T_SILENT = 0.85
T_PREVIEW = 0.65
T_PROMPT = 0.45


@dataclass
class Decision:
    """Router output. mode in: silent | preview | prompt | fallback."""
    mode: str
    wizard_name: str | None
    example_id: int | None
    score: float
    weight: float
    matched_text: str | None
    reason: str


class Router:
    def __init__(self, store: ContextStore):
        self.store = store

    def seed(self) -> int:
        """Embed HANDLER_SEEDS into the examples table. Idempotent.
        Returns number of NEW rows written.
        """
        existing = {
            (row["text"], row["wizard_name"])
            for row in self.store._conn.execute(
                "SELECT text, wizard_name FROM examples WHERE source='seed'"
            ).fetchall()
        }
        written = 0
        for wizard_name, phrases in HANDLER_SEEDS.items():
            for phrase in phrases:
                if (phrase, wizard_name) in existing:
                    continue
                vec = embed(phrase)
                rid = self.store.store_example(
                    text=phrase,
                    embedding=vec,
                    wizard_name=wizard_name,
                    source="seed",
                    weight=1.0,
                )
                if rid is not None:
                    written += 1
        return written

    def decide(self, user_input: str) -> Decision:
        if not user_input or not user_input.strip():
            return Decision(
                mode="fallback",
                wizard_name=None,
                example_id=None,
                score=0.0,
                weight=0.0,
                matched_text=None,
                reason="empty input",
            )

        vec = embed(user_input)
        match = self.store.match_example(vec, min_score=0.0)
        if match is None:
            return Decision(
                mode="fallback",
                wizard_name=None,
                example_id=None,
                score=0.0,
                weight=0.0,
                matched_text=None,
                reason="no examples in table",
            )

        score = match["score"]
        if score >= T_SILENT:
            mode = "silent"
            reason = f"raw cosine {score:.3f} >= {T_SILENT}"
        elif score >= T_PREVIEW:
            mode = "preview"
            reason = f"raw cosine {score:.3f} in [{T_PREVIEW}, {T_SILENT})"
        elif score >= T_PROMPT:
            mode = "prompt"
            reason = f"raw cosine {score:.3f} in [{T_PROMPT}, {T_PREVIEW})"
        else:
            mode = "fallback"
            reason = f"raw cosine {score:.3f} < {T_PROMPT}"

        return Decision(
            mode=mode,
            wizard_name=match["wizard_name"],
            example_id=match["id"],
            score=score,
            weight=match["weight"],
            matched_text=match["text"],
            reason=reason,
        )

    def record(self, decision: Decision, success: bool) -> None:
        if decision.example_id is None:
            return
        if success:
            self.store.record_win(decision.example_id)
        else:
            self.store.record_loss(decision.example_id)
