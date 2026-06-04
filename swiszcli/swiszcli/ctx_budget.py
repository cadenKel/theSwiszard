"""Context window accounting.

Cheap, deterministic. Token estimate = len(text)//4 (good enough for
budget alarms). Two thresholds:

    SOFT = 32_000    rolling target; agent should compact
    HARD = 64_000    fail loud beyond this, do not silently truncate

Caller passes the assembled prompt + history, get back a verdict.
"""
from __future__ import annotations

from dataclasses import dataclass


SOFT = 32_000
HARD = 64_000


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Verdict:
    tokens: int
    soft: bool   # over soft threshold
    hard: bool   # over hard threshold

    def __bool__(self) -> bool:
        return self.hard


def check(text: str, soft: int = SOFT, hard: int = HARD) -> Verdict:
    t = estimate_tokens(text)
    return Verdict(tokens=t, soft=t > soft, hard=t > hard)


class ContextOverflow(RuntimeError):
    """Raised when assembled context exceeds hard cap."""


def enforce(text: str, *, soft: int = SOFT, hard: int = HARD) -> Verdict:
    v = check(text, soft=soft, hard=hard)
    if v.hard:
        raise ContextOverflow(
            f"context {v.tokens} tok exceeds HARD={hard}; compact or split"
        )
    return v
