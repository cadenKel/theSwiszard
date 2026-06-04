"""P1 one-tap learning: observational.

When the LLM emits a swiszard call that SUCCEEDS, we have a free labeled
pair (user_text, wizard_name). The wizard_name is inferred from the
swiszard DSL prefix. We embed user_text and store as a learned example
(source=learned). On the next paraphrase, the router will recognise it.

NO LLM in this module. Pure pattern match + sqlite write.
"""
from __future__ import annotations

import re

_PATTERNS = [
    (re.compile(r"^\s*read\s+/", re.I),                                "read"),
    (re.compile(r"^\s*grep\s+", re.I),                                 "grep"),
    (re.compile(r"^\s*find\s+files?\s+matching\s+\S+\s+in\s+", re.I),  "find_files"),
    (re.compile(r"^\s*find\s+\S+\s+in\s+", re.I),                      "find_files"),
    (re.compile(r"^\s*run\s+\x60", re.I),                              "shell"),
    (re.compile(r"^\s*search\s+the\s+web\s+for\s+", re.I),             "research"),
    (re.compile(r"^\s*memory\s+remember\s+", re.I),                    "remember"),
    (re.compile(r"^\s*memory\s+recall\b", re.I),                       "recall"),
    (re.compile(r"^\s*memory\s+show\s+\d+", re.I),                     "recall"),
    (re.compile(r"^\s*write_b64\s+/", re.I),                           "shell"),
]


def infer_wizard(swiszard_task):
    if not swiszard_task:
        return None
    t = swiszard_task.strip()
    for rx, label in _PATTERNS:
        if rx.match(t):
            return label
    return None


class Learner:
    DEDUP_THRESHOLD = 0.92

    def __init__(self, store):
        self.store = store

    def observe(self, user_text, swiszard_task, *, success):
        if not user_text or not user_text.strip():
            return {"action": "skip", "reason": "empty user_text"}
        wizard = infer_wizard(swiszard_task)
        if wizard is None:
            return {"action": "skip", "reason": "unrecognized swiszard pattern",
                    "task_head": swiszard_task[:60]}
        from .embed import embed
        try:
            vec = embed(user_text)
        except Exception as e:
            return {"action": "error", "reason": "embed failed: " + str(e)}
        # match_example returns a single best-or-None dict.
        best = self.store.match_example(vec, min_score=0.0)
        if best is not None and best["wizard_name"] == wizard and best["score"] >= self.DEDUP_THRESHOLD:
            if success:
                self.store.record_win(best["id"])
                return {"action": "reinforce", "wizard": wizard,
                        "id": best["id"], "score": best["score"]}
            else:
                self.store.record_loss(best["id"])
                return {"action": "downweight", "wizard": wizard,
                        "id": best["id"], "score": best["score"]}
        weight = 1.0 if success else 0.5
        ex_id = self.store.store_example(
            text=user_text,
            embedding=vec,
            wizard_name=wizard,
            source="learned",
            weight=weight,
        )
        return {"action": "learn", "wizard": wizard, "id": ex_id, "weight": weight}
