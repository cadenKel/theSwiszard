"""Scratchpad wizards: LLM interface to external memory.

DSL handlers the LLM emits to interact with its scratchpad. Added to
the existing wizard registry. Taught via system prompt to use these for
any multi-step task:

  plan: GOAL | step1 | step2 | step3       -> creates scratchpad
  observe: action ## result                 -> log what happened
  done                                      -> mark current step complete
  done: result summary                      -> same + summary
  decide: choice ## why                     -> record a decision
  blocker: text                             -> note a blocker
  insert: text                              -> insert new step after current
  scratchpad                                -> show current scratchpad
  abandon: reason                           -> abandon current plan

Output is what the scratchpad LOOKS LIKE after the operation, so the LLM
sees its updated structured memory immediately.
"""
from __future__ import annotations


class ScratchpadOps:
    """Stateless ops over a ScratchpadStore. Returns rendered scratchpad."""

    def __init__(self, store, session_id):
        self.store = store
        self.session_id = session_id

    def _get_or_none(self):
        return self.store.get_active(self.session_id)

    def plan(self, goal, steps):
        existing = self._get_or_none()
        if existing and not existing.is_done:
            existing.abandon("superseded by new plan")
        sp = self.store.create(self.session_id, goal, steps)
        return "PLAN CREATED" + chr(10) + sp.render()

    def observe(self, action, result):
        sp = self._get_or_none()
        if not sp:
            return "ERROR: no active scratchpad. Use plan: GOAL | step1 | step2"
        sp.observe(action, result)
        return sp.render()

    def done(self, result_summary=""):
        sp = self._get_or_none()
        if not sp:
            return "ERROR: no active scratchpad"
        sp.complete_step(result_summary)
        return sp.render()

    def decide(self, choice, why):
        sp = self._get_or_none()
        if not sp:
            return "ERROR: no active scratchpad"
        sp.add_decision(choice, why)
        return sp.render()

    def blocker(self, text):
        sp = self._get_or_none()
        if not sp:
            return "ERROR: no active scratchpad"
        sp.add_blocker(text)
        return sp.render()

    def insert(self, text):
        sp = self._get_or_none()
        if not sp:
            return "ERROR: no active scratchpad"
        sp.insert_step(text)
        return sp.render()

    def show(self):
        sp = self._get_or_none()
        if not sp:
            return "(no active scratchpad)"
        return sp.render()

    def abandon(self, reason):
        sp = self._get_or_none()
        if not sp:
            return "ERROR: no active scratchpad to abandon"
        sp.abandon(reason)
        return "ABANDONED" + chr(10) + sp.render()


def parse_and_dispatch(task, ops):
    """Parse a swiszard-style scratchpad task and dispatch to ops.

    Returns (handled: bool, output: str).
    """
    t = task.strip()
    lo = t.lower()

    if lo == "scratchpad" or lo == "show scratchpad":
        return True, ops.show()

    if lo.startswith("plan:"):
        body = t[len("plan:"):].strip()
        parts = [p.strip() for p in body.split("|")]
        if len(parts) < 2:
            return True, "ERROR: plan: GOAL | step1 | step2 | ..."
        goal, steps = parts[0], parts[1:]
        return True, ops.plan(goal, steps)

    if lo.startswith("observe:"):
        body = t[len("observe:"):].strip()
        if "##" in body:
            action, result = [p.strip() for p in body.split("##", 1)]
        else:
            action, result = body, ""
        return True, ops.observe(action, result)

    if lo == "done":
        return True, ops.done()
    if lo.startswith("done:"):
        return True, ops.done(t[len("done:"):].strip())

    if lo.startswith("decide:"):
        body = t[len("decide:"):].strip()
        if "##" not in body:
            return True, "ERROR: decide: CHOICE ## WHY"
        choice, why = [p.strip() for p in body.split("##", 1)]
        return True, ops.decide(choice, why)

    if lo.startswith("blocker:"):
        return True, ops.blocker(t[len("blocker:"):].strip())

    if lo.startswith("insert:"):
        return True, ops.insert(t[len("insert:"):].strip())

    if lo.startswith("abandon:"):
        return True, ops.abandon(t[len("abandon:"):].strip())
    if lo == "abandon":
        return True, ops.abandon("user request")

    return False, ""
