# Built-in wizards. Each wizard is just data; the runner drives the UI.
# Add a new wizard = add a function below + call register(make_xxx(mem)).

from __future__ import annotations

from typing import Any

from .memory import MemoryClient
from .wizard import Choice, Step, Wizard, register


# ── helpers ─────────────────────────────────────────────────────────────────

def _memory_choices(mem: MemoryClient, limit: int = 100):
    def fn(ctx):
        data = mem.list_memories(limit=limit)
        rows = data.get("memories", []) if isinstance(data, dict) else data
        out = []
        for row in rows:
            mid = row.get("id")
            content = (row.get("content") or "").replace("\n", " ")
            label = f"#{mid}  {content[:80]}"
            preview = content[:600]
            out.append(Choice(value=mid, label=label, preview=preview))
        return out
    return fn


def _trigger_choices(mem: MemoryClient):
    def fn(ctx):
        mid = ctx.get("memory_id")
        if mid is None:
            return []
        data = mem.trigger_list(int(mid))
        out = []
        for t in data.get("triggers", []):
            label = f"[{t["id"]}]  {t["text"][:90]}"
            out.append(Choice(value=t["id"], label=label, preview=t["text"]))
        return out
    return fn


# ── memory wizards ──────────────────────────────────────────────────────────

def register_all(mem: MemoryClient) -> None:
    # /mem.remember
    register(Wizard(
        name="mem.remember",
        title="remember a new fact",
        steps=[
            Step(key="content", kind="text", prompt="what is the fact?", multiline=True,
                 validate=lambda v, c: None if v.strip() else "content cannot be empty"),
            Step(key="tags", kind="text", prompt="tags (comma-separated, optional)", default=""),
            Step(key="confirm", kind="confirm", prompt="save it?"),
        ],
        commit=lambda ctx: (
            None if not ctx.get("confirm")
            else mem.remember(
                ctx["content"].strip(),
                tags=[t.strip() for t in ctx["tags"].split(",") if t.strip()],
            )
        ),
    ))

    # /mem.show
    register(Wizard(
        name="mem.show",
        title="show a memory",
        steps=[
            Step(key="memory_id", kind="pick", prompt="pick a memory",
                 choices=_memory_choices(mem)),
        ],
        commit=lambda ctx: mem.show(int(ctx["memory_id"])),
    ))

    # /mem.pin
    register(Wizard(
        name="mem.pin",
        title="pin a memory (always-inject)",
        steps=[
            Step(key="memory_id", kind="pick", prompt="pick a memory to pin",
                 choices=_memory_choices(mem)),
        ],
        commit=lambda ctx: mem.pin(int(ctx["memory_id"])),
    ))

    # /mem.unpin
    register(Wizard(
        name="mem.unpin",
        title="unpin a memory",
        steps=[
            Step(key="memory_id", kind="pick", prompt="pick a memory to unpin",
                 choices=_memory_choices(mem)),
        ],
        commit=lambda ctx: mem.unpin(int(ctx["memory_id"])),
    ))

    # /mem.forget  (destructive — confirm required)
    register(Wizard(
        name="mem.forget",
        title="permanently delete a memory",
        steps=[
            Step(key="memory_id", kind="pick", prompt="pick a memory to FORGET",
                 choices=_memory_choices(mem)),
            Step(key="confirm", kind="confirm",
                 prompt="this is PERMANENT. proceed?"),
        ],
        commit=lambda ctx: mem.forget(int(ctx["memory_id"])) if ctx.get("confirm") else None,
    ))

    # /mem.trigger.add  (append-only)
    register(Wizard(
        name="mem.trigger.add",
        title="add a trigger to a memory (append-only)",
        steps=[
            Step(key="memory_id", kind="pick", prompt="pick memory to add trigger to",
                 choices=_memory_choices(mem)),
            Step(key="trigger_text", kind="text",
                 prompt="trigger phrase (a situation/question that should fire this memory)",
                 placeholder="e.g. when asked about swizmem",
                 validate=lambda v, c: None if v.strip() else "trigger cannot be empty"),
            Step(key="confirm", kind="confirm", prompt="add this trigger?"),
        ],
        commit=lambda ctx: (
            None if not ctx.get("confirm")
            else mem.trigger_add(int(ctx["memory_id"]), ctx["trigger_text"].strip())
        ),
    ))

    # /mem.trigger.remove  (the ONLY way to delete a trigger)
    register(Wizard(
        name="mem.trigger.remove",
        title="remove a trigger from a memory",
        steps=[
            Step(key="memory_id", kind="pick", prompt="pick memory",
                 choices=_memory_choices(mem)),
            Step(key="trigger_id", kind="pick", prompt="pick trigger to remove",
                 choices=_trigger_choices(mem)),
            Step(key="confirm", kind="confirm", prompt="remove this trigger?"),
        ],
        commit=lambda ctx: (
            None if not ctx.get("confirm")
            else mem.trigger_remove(int(ctx["trigger_id"]))
        ),
    ))

    # /mem.trigger.list  (read-only)
    register(Wizard(
        name="mem.trigger.list",
        title="list triggers on a memory",
        steps=[
            Step(key="memory_id", kind="pick", prompt="pick memory",
                 choices=_memory_choices(mem)),
        ],
        commit=lambda ctx: mem.trigger_list(int(ctx["memory_id"])),
    ))


    # --- NEW /mem CRUD additions ---

    # /mem search: query -> top 20 semantic hits
    def _do_search(ctx):
        query = (ctx.get("query") # user-typed
                 or "").strip()
        if not query:
            return {"results": []}
        res = mem.recall_content(query, top_k=20)
        return {"query": query, "results": res}

    register(Wizard(
        name="mem.search",
        title="search memories (semantic)",
        steps=[
            Step(key="query", kind="text",
                 prompt="search query (will be embedded)",
                 validate=lambda v, c: None if v.strip() else "query cannot be empty"),
            Step(key="_run", kind="action", prompt="", action=_do_search),
        ],
        commit=lambda ctx: ctx.get("_run"),
    ))

    # /mem.deprecate
    register(Wizard(
        name="mem.deprecate",
        title="deprecate (soft-delete) a memory",
        steps=[
            Step(key="memory_id", kind="pick", prompt="pick a memory",
                 choices=_memory_choices(mem)),
            Step(key="reason", kind="text",
                 prompt="reason (optional)", default=""),
            Step(key="confirm", kind="confirm", prompt="deprecate?"),
        ],
        commit=lambda ctx: (
            None if not ctx.get("confirm")
            else mem.deprecate(int(ctx["memory_id"]),
                              ctx.get("reason") or None)
        ),
    ))

    # /mem.update_content: supersede with new wording (server auto-gens
    # triggers if you don't pass any). existing triggers are edited
    # separately via /mem.trigger.add / / remove (append-only invariant).
    def _prefill_current(ctx):
        mid = ctx.get("memory_id")
        if mid is None:
            return ""
        row = mem.show(int(mid))
        # /show returns {"memory": {...}} OR {...} directly depending on server version
        body = row.get("memory") if isinstance(row, dict) and isinstance(row.get("memory"), dict) else row
        content = (body or {}).get("content", "") if isinstance(body, dict) else ""
        ctx["__default__new_content"] = content
        return content

    register(Wizard(
        name="mem.update_content",
        title="rewrite a memory (supersedes old one)",
        steps=[
            Step(key="memory_id", kind="pick", prompt="pick a memory",
                 choices=_memory_choices(mem)),
            Step(key="_prefill", kind="action", prompt="",
                 action=_prefill_current),
            Step(key="new_content", kind="text", multiline=True,
                 prompt="new content (edit as needed)",
                 default="",  # filled by _prefill action above via ctx
                 validate=lambda v, c: None if v.strip() else "content cannot be empty"),
            Step(key="lesson", kind="text",
                 prompt="lesson (optional, why did we rewrite?)", default=""),
            Step(key="confirm", kind="confirm",
                 prompt="supersede old memory with this content?"),
        ],
        commit=lambda ctx: (
            None if not ctx.get("confirm")
            else mem.supersede(
                int(ctx["memory_id"]),
                ctx["new_content"].strip(),
                lesson=(ctx.get("lesson") or None),
            )
        ),
    ))
