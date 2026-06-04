"""Meta-wizards: the palace grows itself.

wizard.research    structured evidence gathering.
wizard.author      builds a new Wizard interactively, persists via store.

Only references whitelisted callables (swiszcli.callables) so the
meta-layer composes with phase 5 wizards-as-data.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from . import callables as wcall
from . import pools
from . import wizard_store
from .wizard import Step, Wizard, register


def _seed_research_sources() -> None:
    p = pools.get_pool("research.sources")
    p.seed([
        ("swizmem.recall",   "swizmem semantic recall"),
        ("trace.search",     "search prior wizard traces"),
        ("file.grep",        "grep a path on disk"),
        ("swiszard.command", "run arbitrary swiszard DSL"),
        ("web.search",       "searxng web search"),
    ])


def _seed_step_kinds() -> None:
    p = pools.get_pool("wizard.step_kinds")
    p.seed([
        ("text",        "freeform string"),
        ("confirm",     "yes/no"),
        ("action",      "run a whitelisted action callable"),
        ("pick_or_new", "top-N from a ChoicePool + new option"),
    ])


def _swizmem_url() -> str:
    return os.environ.get("SWISZMEM_URL", "http://127.0.0.1:7437")


def _swizmem_recall(query: str, n: int = 5) -> list[dict]:
    url = _swizmem_url() + "/recall?" + urllib.parse.urlencode({"q": query, "n": n})
    try:
        with urllib.request.urlopen(url, timeout=3.0) as r:
            data = json.loads(r.read().decode())
    except Exception as ex:
        return [{"source": "swizmem.recall", "error": str(ex)}]
    out = []
    raw = data.get("results", []) if isinstance(data, dict) else data
    for m in raw or []:
        out.append({"source": "swizmem.recall",
                    "id": m.get("id"),
                    "score": m.get("score"),
                    "text": (m.get("content") or m.get("text") or "")[:400]})
    return out


@wcall.action("research.run")
def _research_run(ctx: dict) -> list[dict]:
    topic = (ctx.get("topic") or "").strip()
    sources = ctx.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    snippets: list[dict] = []
    for src in sources:
        if src == "swizmem.recall":
            snippets.extend(_swizmem_recall(topic, n=5))
        else:
            snippets.append({
                "source": src,
                "note": "not wired yet; add a callable in swiszcli.callables",
                "topic": topic,
            })
    return snippets


@wcall.action("research.summarize")
def _research_summarize(ctx: dict) -> dict:
    snippets = ctx.get("run") or []
    return {"topic": ctx.get("topic"),
            "n_snippets": len(snippets),
            "snippets": snippets}


def _build_research() -> Wizard:
    return Wizard(
        name="wizard.research",
        title="research a topic across sources",
        steps=[
            Step(key="topic", kind="text",
                 prompt="what are we researching?"),
            Step(key="sources", kind="pick_or_new",
                 prompt="which source?",
                 pool="research.sources", top_n=8),
            Step(key="run", kind="action", prompt="collecting snippets",
                 action=wcall.ACTIONS["research.run"]),
            Step(key="bundle", kind="action", prompt="bundling evidence",
                 action=wcall.ACTIONS["research.summarize"]),
        ],
    )


@wcall.action("author.add_step")
def _author_add_step(ctx: dict) -> list[dict]:
    desc = {
        "key": ctx.get("step_key") or "step",
        "kind": ctx.get("step_kind") or "text",
        "prompt": ctx.get("step_prompt") or "?",
    }
    if desc["kind"] == "pick_or_new":
        desc["pool"] = ctx.get("step_pool") or ""
        if ctx.get("step_top_n"):
            desc["top_n"] = int(ctx["step_top_n"])
    if desc["kind"] == "action":
        desc["action_ref"] = ctx.get("step_action_ref") or ""
    steps = list(ctx.get("steps") or [])
    steps.append(desc)
    return steps


@wcall.action("author.commit")
def _author_commit(ctx: dict) -> dict:
    name = (ctx.get("name") or "").strip()
    title = (ctx.get("title") or "").strip()
    steps_raw = ctx.get("steps") or []
    if not name or not title:
        raise ValueError("author.commit: name and title required")
    if not steps_raw:
        raise ValueError("author.commit: at least one step required")
    steps: list[Step] = []
    for s in steps_raw:
        kind = s["kind"]
        kw: dict = {"key": s["key"], "kind": kind, "prompt": s.get("prompt", "?")}
        if kind == "pick_or_new":
            kw["pool"] = s.get("pool") or (name + "." + s["key"])
            kw["top_n"] = int(s.get("top_n") or 8)
        if kind == "action":
            ref = s.get("action_ref")
            if not ref or ref not in wcall.ACTIONS:
                raise KeyError("author.commit: unknown action_ref " + repr(ref))
            kw["action"] = wcall.ACTIONS[ref]
        steps.append(Step(**kw))
    wiz = Wizard(name=name, title=title, steps=steps)
    store = wizard_store.get_default()
    if store is None:
        raise RuntimeError("WizardStore not initialized")
    store.save(wiz, source=ctx.get("__source__", "llm"))
    return {"saved": name, "n_steps": len(steps)}


@wcall.next_("author.maybe_commit")
def _author_maybe_commit(ctx: dict):
    return "result" if ctx.get("commit_now") else None


def _build_author() -> Wizard:
    """Single-shot author: one step captured per invocation.

    Multi-step wizards are built by repeated invocations sharing the
    same name + title. Each invocation appends a step (via
    author.add_step) and optionally commits.
    """
    return Wizard(
        name="wizard.author",
        title="author a new wizard (one step per invocation)",
        steps=[
            Step(key="name",   kind="text", prompt="wizard name (dotted, e.g. notes.add)"),
            Step(key="title",  kind="text", prompt="title"),
            Step(key="step_key",    kind="text", prompt="step key"),
            Step(key="step_kind",   kind="pick_or_new",
                 prompt="step kind?", pool="wizard.step_kinds", top_n=8),
            Step(key="step_prompt", kind="text", prompt="step prompt"),
            Step(key="steps", kind="action", prompt="adding step",
                 action=wcall.ACTIONS["author.add_step"]),
            Step(key="commit_now", kind="confirm",
                 prompt="commit this wizard now? (n = leave draft in trace)",
                 next=wcall.NEXT["author.maybe_commit"]),
            Step(key="result", kind="action", prompt="committing",
                 action=wcall.ACTIONS["author.commit"]),
        ],
    )


def register_meta_wizards() -> None:
    _seed_research_sources()
    _seed_step_kinds()
    register(_build_research())
    register(_build_author())
