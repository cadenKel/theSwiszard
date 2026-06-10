"""Headless WizardRunner driven by a scripted input list.

Used for tests and (later) for the LLM to walk wizards programmatically.
The "script" is just a list of values consumed in step order. For pick
and multi steps, values must match a Choice.value. For pick_or_new, you
can either pass an existing Choice.value or the literal new value (the
runner detects the pool and appends if missing).

NO fallbacks. If the script runs out, raise. If a scripted value is not
a valid choice, raise.
"""
from __future__ import annotations

from typing import Any, Iterator

from .wizard import Cancelled, Choice, Step, Wizard, WizardRunner


class ScriptRunner(WizardRunner):
    def __init__(self, script: list[Any]) -> None:
        self._script: Iterator[Any] = iter(script)

    def _next(self, step: Step) -> Any:
        try:
            return next(self._script)
        except StopIteration:
            raise RuntimeError(f"script exhausted at step {step.key!r}")

    def do_text(self, wiz: Wizard, step: Step, ctx: dict) -> str:
        val = str(self._next(step))
        if step.validate:
            err = step.validate(val, ctx)
            if err:
                raise ValueError(f"scripted value for {step.key!r} failed validation: {err}")
        return val

    def do_confirm(self, wiz: Wizard, step: Step, ctx: dict) -> bool:
        return bool(self._next(step))

    def do_action(self, wiz: Wizard, step: Step, ctx: dict) -> Any:
        if step.action is None:
            raise RuntimeError(f"action step {step.key!r} has no action fn")
        return step.action(ctx)

    def do_nested(self, wiz: Wizard, step: Step, ctx: dict) -> Any:
        from .wizard import resolve
        from copy import deepcopy
        if not step.nested_wizard:
            raise RuntimeError(f"nested step {step.key!r} missing nested_wizard")
        sub = resolve(step.nested_wizard)
        return sub.run(self, initial=deepcopy(ctx),
                       parent_trace_id=ctx.get("__trace_id__"),
                       source=ctx.get("__source__"))

    def do_pick(self, wiz: Wizard, step: Step, ctx: dict) -> Any:
        if step.choices is None:
            raise RuntimeError(f"pick step {step.key!r} has no choices fn")
        choices: list[Choice] = step.choices(ctx)
        want = self._next(step)
        for c in choices:
            if c.value == want:
                return c.value
        raise ValueError(f"scripted pick {want!r} not in {[c.value for c in choices]}")

    def do_multi(self, wiz: Wizard, step: Step, ctx: dict) -> list:
        if step.choices is None:
            raise RuntimeError(f"multi step {step.key!r} has no choices fn")
        choices: list[Choice] = step.choices(ctx)
        wants = self._next(step)
        if not isinstance(wants, list):
            raise TypeError(f"multi step {step.key!r} expects list, got {type(wants).__name__}")
        valid = {c.value for c in choices}
        bad = [w for w in wants if w not in valid]
        if bad:
            raise ValueError(f"scripted multi {bad!r} not in {sorted(valid)!r}")
        return wants

    def do_pick_or_new(self, wiz: Wizard, step: Step, ctx: dict) -> Any:
        from . import pools
        if not step.pool:
            raise RuntimeError(f"pick_or_new step {step.key!r} missing pool name")
        pool = pools.get_pool(step.pool)
        want = self._next(step)
        existing = pool.find(str(want))
        if existing:
            pool.touch(existing.value)
            return existing.value
        # new value: append + touch
        created_by = ctx.get("__source__", "user")
        pool.add(str(want), label=str(want), created_by=created_by)
        pool.touch(str(want))
        return str(want)
