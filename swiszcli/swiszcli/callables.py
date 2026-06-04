"""Whitelisted callables for data-authored wizards.

Wizards persisted as JSON (LLM- or user-authored) cannot reference
arbitrary Python lambdas. They can only reference names registered
here. This is the security boundary between palace-as-data and
arbitrary code execution.

Three registries:
- CHOICES    : ChoicesFn   (ctx) -> list[Choice]
- VALIDATORS : ValidateFn  (val, ctx) -> str|None  (None == ok)
- ACTIONS    : Callable    (ctx) -> Any   (used for kind=action)
- NEXT       : NextFn      (ctx) -> str|None
"""
from __future__ import annotations

from typing import Any, Callable

CHOICES: dict[str, Callable] = {}
VALIDATORS: dict[str, Callable] = {}
ACTIONS: dict[str, Callable] = {}
NEXT: dict[str, Callable] = {}


def _reg(table: dict, name: str):
    def deco(fn):
        if name in table:
            raise ValueError(f"callable {name!r} already registered")
        table[name] = fn
        return fn
    return deco


def choices(name: str):  return _reg(CHOICES, name)
def validator(name: str): return _reg(VALIDATORS, name)
def action(name: str):    return _reg(ACTIONS, name)
def next_(name: str):     return _reg(NEXT, name)


# ── default actions usable by data wizards ───────────────────────────────
@action("noop")
def _noop(ctx: dict) -> Any:
    return None


@action("ctx.dump")
def _ctx_dump(ctx: dict) -> dict:
    """Return a copy of ctx minus framework keys (useful for previews)."""
    return {k: v for k, v in ctx.items() if not k.startswith("__")}


@validator("nonempty")
def _nonempty(val: Any, ctx: dict) -> str | None:
    if not (isinstance(val, str) and val.strip()):
        return "must not be empty"
    return None
