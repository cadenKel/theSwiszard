# swiszCLI wizard engine.
#
# ONE primitive (Wizard) drives every slash command and every LLM-triggered
# modal interaction. Wizards are composed of Steps. Step kinds:
#   pick     - fuzzy-filterable list, arrow keys navigate
#   text     - freeform string with optional validator
#   confirm  - y/n
#   multi    - checkbox list
#   nested   - open another wizard, return its result
#   action   - no UI, runs ctx -> value (fetch / side-effect)
#   pick_or_new - top-N from a persistent ChoicePool + "+ new" row;
#                 picking new prompts text, then APPENDS to the pool.
#                 This is the primitive the memory palace is built from.
#
# Wizards register by dotted name (REGISTRY) so /mem.trigger.add and the LLM
# tool call "wizard mem.trigger.add" resolve to the same code path.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

REGISTRY: dict[str, "Wizard"] = {}


def register(wiz: "Wizard") -> "Wizard":
    if wiz.name in REGISTRY:
        raise ValueError(f"wizard already registered: {wiz.name}")
    REGISTRY[wiz.name] = wiz
    return wiz


def resolve(name: str) -> "Wizard":
    if name not in REGISTRY:
        raise KeyError(f"no such wizard: {name!r}. known: {sorted(REGISTRY)}")
    return REGISTRY[name]


def list_wizards(prefix: str = "") -> list[str]:
    return sorted(n for n in REGISTRY if n.startswith(prefix))


class Cancelled(Exception):
    pass


@dataclass
class Choice:
    value: Any
    label: str
    preview: str = ""


ChoicesFn = Callable[[dict], list[Choice]]
ValidateFn = Callable[[Any, dict], "str | None"]
NextFn = Callable[[dict], "str | None"]


@dataclass
class Step:
    key: str
    kind: str
    prompt: str
    choices: ChoicesFn | None = None
    default: Any = None
    validate: ValidateFn | None = None
    next: NextFn | None = None
    nested_wizard: str | None = None
    action: Callable[[dict], Any] | None = None
    multiline: bool = False
    placeholder: str = ""
    pool: str | None = None              # for kind="pick_or_new": pool name
    top_n: int = 10                       # for pick_or_new
    new_prompt: str = "type a new option" # for pick_or_new


@dataclass
class Wizard:
    name: str
    title: str
    steps: list[Step]
    summary: Callable[[dict], str] | None = None
    commit: Callable[[dict], Any] | None = None

    def step_by_key(self, key: str) -> Step:
        for s in self.steps:
            if s.key == key:
                return s
        raise KeyError(f"no step {key!r} in wizard {self.name}")

    def run(self, runner: "WizardRunner", initial: dict | None = None,
            *, trace_writer=None, parent_trace_id: str | None = None,
            source: str | None = None) -> Any:
        # Trace defaults: try module-level singleton if no explicit writer.
        if trace_writer is None:
            try:
                from . import trace as _trace
                trace_writer = _trace.get_default()
            except Exception:
                trace_writer = None
        ctx: dict = dict(initial or {})
        ctx["__wizard__"] = self.name
        if source is None:
            source = ctx.get("__source__", "user")
        ctx["__source__"] = source
        trace_id = None
        if trace_writer is not None:
            trace_id = trace_writer.start(
                self.name, source, parent_id=parent_trace_id, initial_ctx=initial or {})
            ctx["__trace_id__"] = trace_id
        status = "ok"
        result: Any = None
        try:
            cursor = self.steps[0].key
            while cursor is not None:
                step = self.step_by_key(cursor)
                # __skip_prefilled__: if initial ctx already supplied this step'key
                # with a non-None value, reuse it instead of re-prompting.
                if step.key in ctx and ctx[step.key] is not None and ctx[step.key] != "":
                    value = ctx[step.key]
                else:
                    value = runner.run_step(self, step, ctx)
                ctx[step.key] = value
                if step.next:
                    cursor = step.next(ctx)
                else:
                    idx = self.steps.index(step)
                    cursor = self.steps[idx + 1].key if idx + 1 < len(self.steps) else None
            if self.commit:
                result = self.commit(ctx)
            else:
                result = ctx
            return result
        except Cancelled:
            status = "cancelled"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            if trace_writer is not None and trace_id is not None:
                trace_writer.end(trace_id, ctx, result, status)


class WizardRunner:
    def run_step(self, wiz: Wizard, step: Step, ctx: dict) -> Any:
        method = getattr(self, f"do_{step.kind}", None)
        if method is None:
            raise NotImplementedError(f"runner has no do_{step.kind}")
        return method(wiz, step, ctx)
