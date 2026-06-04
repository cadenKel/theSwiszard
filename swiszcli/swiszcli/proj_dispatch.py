"""Deterministic prefix dispatcher for project verbs.

This mirrors mem_dispatch.py but for the mind-palace project substrate.
The swiszard DSL gains project verbs that the LLM can emit via <<SWISZ>>
blocks, routed here before hitting the swiszard TF-IDF router.

Verbs:
  project list
  project create <name>
  project add <project> <body> [kind=objective] [state=proposed] [tags=a,b] [triggers=t1;t2]
  project status <project>
  project tree <project>
  project inject <project> <query>
  project conflicts [project]
  project resolve <id> <resolution>
  project transition <id> <state>
"""
from __future__ import annotations

import re
from typing import Any

# Share one client per session to avoid creating new connections per call.
_CLIENT = None


def _get_client(base_url: str, timeout: float = 10.0):
    global _CLIENT
    if _CLIENT is None:
        from .swiszproj import ProjectClient
        _CLIENT = ProjectClient(base_url, timeout=timeout)
    return _CLIENT


def _reset_client():
    global _CLIENT
    _CLIENT = None


def try_project_dispatch(task: str, mem_url: str) -> str | None:
    """If task starts with 'project ', dispatch to ProjectClient.
    Returns None if not a project verb (so the caller can fall through)."""
    TASK = (task or "").strip()
    if not TASK.startswith("project "):
        return None
    rest = TASK[len("project "):].strip()
    if not rest:
        return (
            "project: needs a verb. Supported: list, create, add, status, "
            "tree, inject, conflicts, resolve, transition."
        )
    verb, _, rest = rest.partition(" ")
    verb = verb.lower()
    rest = rest.strip()

    pc = _get_client(mem_url)

    try:
        if verb == "list":
            projects = pc.list()
            if not projects:
                return "project list: no projects yet."
            lines = [f"project list: {len(projects)} project(s)"]
            for p in projects:
                lines.append(
                    f"  #{p.get('id')}  {p.get('name'):20s}  nodes={p.get('node_count',0)}"
                )
            return "\n".join(lines)

        if verb == "create":
            if not rest:
                return "project create: needs a name."
            name = rest.strip()
            result = pc.create(name)
            return f"project create: #{result.get('id')} {result.get('name')}"

        if verb == "add":
            # Parse: project add <project_name> <body> [kind=X] [state=X] [tags=a,b] [triggers=t1;t2]
            parts = rest.split(None, 1)
            if len(parts) < 2:
                return "project add: needs <project> <body> [kind=objective] [tags=...] [triggers=...]"
            project_name = parts[0]
            remainder = parts[1]

            # Parse optional key=value pairs from the end
            kwargs: dict[str, Any] = {"kind": "objective", "state": "proposed"}
            kv_match = re.findall(r'\b(kind|state|tags|triggers)=("[^"]*"|\S+)', remainder)
            if kv_match:
                for key, val in kv_match:
                    body_end = remainder.rfind(f"{key}={val}")
                    remainder = remainder[:body_end].strip()
                    val = val.strip('"')
                    if key == "tags":
                        kwargs[key] = [t.strip() for t in val.split(",") if t.strip()]
                    elif key == "triggers":
                        kwargs[key] = [t.strip() for t in val.split(";") if t.strip()]
                    else:
                        kwargs[key] = val
            body = remainder.strip()
            if not body:
                return "project add: needs a non-empty body."
            result = pc.add_node(project_name, body, **kwargs)
            nid = result.get("node_id", result.get("id"))
            return f"project add: node #{nid} in '{project_name}' (kind={kwargs.get('kind')})"

        if verb == "status":
            if not rest:
                return "project status: needs a project name."
            result = pc.status(rest)
            import json
            return json.dumps(result, indent=2, ensure_ascii=False)

        if verb == "tree":
            if not rest:
                return "project tree: needs a project name."
            result = pc.tree(rest)
            import json
            return json.dumps(result, indent=2, ensure_ascii=False)

        if verb == "inject":
            parts = rest.split(None, 1)
            if len(parts) < 2:
                return "project inject: needs <project> <query>"
            project_name = parts[0]
            query = parts[1]
            frames = pc.inject(query, active_project=project_name)
            if not frames:
                return f"project inject '{project_name}': 0 frames."
            lines = [f"project inject '{project_name}': {len(frames)} frame(s)"]
            for f in frames:
                title = f.get("title", "?")
                body = (f.get("body", "") or "").replace("\n", " ")[:120]
                score = f.get("score", 0.0)
                nid = f.get("node_id", "?")
                lines.append(f"  [node:{nid} s={score:.2f}] {title}: {body}")
            return "\n".join(lines)

        if verb == "conflicts":
            project = rest if rest else None
            rows = pc.conflicts(project=project)
            if not rows:
                return "project conflicts: none open."
            lines = [f"project conflicts: {len(rows)} open"]
            for r in rows:
                cid = r.get("id")
                sim = r.get("similarity", 0.0)
                a = r.get("title_a") or "?"
                b = r.get("title_b") or "?"
                lines.append(f"  [c:{cid}] sim={sim:.2f}  {a!s:.40} <> {b!s:.40}")
            return "\n".join(lines)

        if verb == "resolve":
            m = re.match(r"^(\d+)\s+(.+)$", rest)
            if not m:
                return "project resolve: needs <conflict_id> <resolution>"
            cid = int(m.group(1))
            resolution = m.group(2).strip()
            result = pc.resolve(cid, resolution)
            return f"project resolve #{cid}: ok"

        if verb == "transition":
            m = re.match(r"^(\d+)\s+(\S+)$", rest)
            if not m:
                return "project transition: needs <node_id> <state>"
            nid = int(m.group(1))
            state = m.group(2)
            result = pc.transition(nid, state)
            return f"project transition #{nid} -> {state}: ok"

    except Exception as ex:
        return f"project {verb} crashed: {type(ex).__name__}: {ex}"

    return f"project {verb}: unknown verb. Supported: list, create, add, status, tree, inject, conflicts, resolve, transition."
