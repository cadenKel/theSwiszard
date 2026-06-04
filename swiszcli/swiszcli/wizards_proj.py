# Project-manager wizards. /proj.* wizards register against a ProjectClient.
#
# proj.add_idea    - capture a node with primitive kinds: pick project, type body
#                    (multiline), optional triggers, save. Conflict scan is
#                    server-side async, so the wizard returns immediately.
# proj.use         - select active project (stored in module-level state)
# proj.new         - create a new project
# proj.conflicts   - walk open conflicts and resolve each one
# proj.tree        - render a project as an indented tree
# proj.inject      - one-off retrieval test (debug)
from __future__ import annotations

from typing import Any

from .swiszproj import ProjectClient
from .wizard import Choice, Step, Wizard, register


# Active project name, set by proj.use. None = ask every time.
ACTIVE: dict[str, Any] = {"project": None}


def get_active() -> str | None:
    return ACTIVE.get("project")


def set_active(name: str | None) -> None:
    ACTIVE["project"] = name


# ── choices ─────────────────────────────────────────────────────────────────

def _project_choices(pc: ProjectClient, *, include_new: bool = False):
    def fn(ctx):
        out = []
        active = get_active()
        for p in pc.list():
            name = p.get("name", "")
            nid = p.get("id")
            mark = " *" if name == active else ""
            out.append(Choice(value=name, label=f"#{nid}  {name}{mark}",
                              preview=name))
        if include_new:
            out.append(Choice(value="__new__", label="+ new project",
                              preview="create a new project"))
        return out
    return fn


def _conflict_choices(pc: ProjectClient):
    def fn(ctx):
        rows = pc.conflicts(project=get_active())
        out = []
        for r in rows:
            cid = r.get("id")
            a = r.get("title_a") or str(r.get("node_a")) or "?"
            b = r.get("title_b") or str(r.get("node_b")) or "?"
            sim = r.get("similarity", 0.0)
            label = f"[c:{cid}] sim={sim:.2f}  {a!s:.40} <> {b!s:.40}"
            out.append(Choice(value=cid, label=label,
                              preview=f"a: {a}\nb: {b}"))
        return out
    return fn


# ── commits ─────────────────────────────────────────────────────────────────

def _commit_add_idea(pc: ProjectClient):
    def commit(ctx):
        if not ctx.get("confirm"):
            return None
        project = ctx.get("project")
        if project == "__new__":
            project = (ctx.get("new_project") or "").strip()
            if not project:
                return {"error": "no project name"}
            pc.create(project)
        body = (ctx.get("body") or "").strip()
        if not body:
            return {"error": "empty body"}
        kind = ctx.get("kind") or "objective"
        tags = [t.strip() for t in (ctx.get("tags") or "").split(",") if t.strip()]
        triggers = [t.strip() for t in (ctx.get("triggers") or "").split(";") if t.strip()]
        result = pc.add_node(project, body, kind=kind, tags=tags, triggers=triggers)
        set_active(project)
        return result
    return commit


def _commit_use(pc: ProjectClient):
    def commit(ctx):
        project = ctx.get("project")
        if project == "__new__":
            project = (ctx.get("new_project") or "").strip()
            if not project:
                return None
            pc.create(project)
        set_active(project)
        return {"active": project}
    return commit


def _commit_new(pc: ProjectClient):
    def commit(ctx):
        name = (ctx.get("name") or "").strip()
        if not name:
            return None
        out = pc.create(name)
        set_active(name)
        return out
    return commit


def _commit_resolve(pc: ProjectClient):
    def commit(ctx):
        cid = ctx.get("conflict_id")
        resolution = (ctx.get("resolution") or "").strip() or "noted"
        if cid is None:
            return None
        return pc.resolve(int(cid), resolution)
    return commit


# ── register ────────────────────────────────────────────────────────────────

def _commit_status(pc: ProjectClient):
    def commit(ctx):
        project = ctx.get("project")
        if not project:
            return None
        result = pc.status(project)
        return result
    return commit

def register_all(pc: ProjectClient) -> None:
    # proj.add_idea -- the headline flow.
    def _project_default(ctx):
        return get_active()

    register(Wizard(
        name="proj.add_idea",
        title="capture a node (no friction)",
        steps=[
            Step(key="project", kind="pick_or_new",
                 prompt="which project?",
                 choices=_project_choices(pc, include_new=True),
                 default=None,
                 pool="proj.projects",
                 new_prompt="new project name"),
            Step(key="new_project", kind="text",
                 prompt="new project name",
                 next=lambda ctx: "kind" if ctx.get("project") != "__new__" else None,
                 default=""),
            Step(key="kind", kind="pick", prompt="what is this?",
                 choices=lambda c: [
                     Choice(value="objective", label="objective - desired outcome / intent"),
                     Choice(value="task",      label="task      - discrete unit of work"),
                     Choice(value="decision",  label="decision  - committed choice + rationale"),
                     Choice(value="question",  label="question  - explicit uncertainty"),
                     Choice(value="artifact",  label="artifact  - concrete thing (file/service/etc)"),
                     Choice(value="note",      label="note      - context / observation"),
                 ],
                 default="objective"),
            Step(key="body", kind="text", prompt="dump it (title = first line)",
                 multiline=True,
                 validate=lambda v, c: None if (v or "").strip() else "body cannot be empty"),
            Step(key="triggers", kind="text",
                 prompt="triggers (situations that should resurface this; semicolon-separated, optional)",
                 default="", placeholder="when planning x; if y comes up"),
            Step(key="tags", kind="text",
                 prompt="tags (comma-separated, optional)", default=""),
            Step(key="confirm", kind="confirm", prompt="save?"),
        ],
        commit=_commit_add_idea(pc),
    ))

    register(Wizard(
        name="proj.new",
        title="create a new project",
        steps=[
            Step(key="name", kind="text", prompt="project name",
                 validate=lambda v, c: None if (v or "").strip() else "name required"),
        ],
        commit=_commit_new(pc),
    ))

    register(Wizard(
        name="proj.use",
        title="set the active project",
        steps=[
            Step(key="project", kind="pick", prompt="active project",
                 choices=_project_choices(pc, include_new=True)),
            Step(key="new_project", kind="text", prompt="new project name",
                 default="",
                 next=lambda ctx: None if ctx.get("project") != "__new__" else "new_project"),
        ],
        commit=_commit_use(pc),
    ))

    register(Wizard(
        name="proj.conflicts",
        title="resolve open conflicts",
        steps=[
            Step(key="conflict_id", kind="pick",
                 prompt="pick a conflict (esc = leave for later)",
                 choices=_conflict_choices(pc)),
            Step(key="resolution", kind="text",
                 prompt="resolution note (merge / supersede / both-valid / free text)",
                 default="noted"),
        ],
        commit=_commit_resolve(pc),
    ))
