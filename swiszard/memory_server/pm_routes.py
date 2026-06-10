"""
pm_routes.py — PM HTTP route handlers for app.py.

Replaces the inline PM routes that were in app.py.
Handles: add_node, tree, node, subtree, status, transition, delete, reparent,
         create, rename, orient, health, tool_call_log.

Removed endpoints (moved to swiszcontext or retired):
  /project/inject       — was pm_frame retrieval; now swiszcontext's job
  /project/recall_triggers — was pm_trigger match; now lesson.trigger_text
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("pm_routes")

# ── request models ────────────────────────────────────────────────────────────

class PMCreateRequest(BaseModel):
    name: str

class PMAddNodeRequest(BaseModel):
    project: str
    body: str
    kind: str = "objective"
    state: str = "proposed"
    parent_id: int | None = None
    tags: list[str] = Field(default_factory=list)
    title: str | None = None
    trigger_text: str = ""
    scan_conflicts: bool = True

class PMTreeRequest(BaseModel):
    project: str

class PMConflictsRequest(BaseModel):
    project: str | None = None

class PMResolveRequest(BaseModel):
    conflict_id: int
    resolution: str

class PMProposeParentRequest(BaseModel):
    project: str
    body: str
    top_k: int = 5

class PMTransitionRequest(BaseModel):
    node_id: int
    state: str

class PMSubtreeRequest(BaseModel):
    root_id: int
    show_all: bool = True

class PMStatusRequest(BaseModel):
    project: str
    max_bottlenecks: int = 5

class PMDeleteRequest(BaseModel):
    node_id: int
    confirmation_token: str
    expected_title: str = ""

class PMReparentRequest(BaseModel):
    node_id: int
    new_parent_id: int

class PMRenameRequest(BaseModel):
    old_name: str
    new_name: str

class PMOrientRequest(BaseModel):
    project: str = "swiszard"

class PMToolCallLogRequest(BaseModel):
    node_id: int
    why: str
    tool_name: str
    tool_args: dict = Field(default_factory=dict)
    result_summary: str = ""
    success: bool = True


# ── route registration ────────────────────────────────────────────────────────

def register_pm_routes(app, _ensure_pm, _get_conn, _pm, pm_backup):
    """Register all PM routes on the FastAPI app."""

    @app.get("/project/list")
    def pm_list():
        _ensure_pm()
        return {"projects": _pm.list_projects(_get_conn())}

    @app.post("/project/create")
    def pm_create(req: PMCreateRequest):
        _ensure_pm()
        conn = _get_conn()
        pid, created = _pm.get_or_create_project_dedup(conn, req.name)
        if created:
            pm_backup.log_mutation("INSERT", "pm_project", pid,
                                   new_row={"name": req.name},
                                   metadata={"endpoint": "/project/create"})
        return {"id": pid, "name": req.name, "created": created}

    @app.post("/project/rename")
    def pm_rename(req: PMRenameRequest):
        _ensure_pm()
        conn = _get_conn()
        try:
            result = _pm.rename_project(conn, req.old_name, req.new_name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        pm_backup.log_mutation("UPDATE", "pm_project", result["project_id"],
                               old_row={"name": req.old_name},
                               new_row={"name": req.new_name},
                               metadata={"endpoint": "/project/rename"})
        return result

    @app.post("/project/add_node")
    def pm_add_node(req: PMAddNodeRequest):
        _ensure_pm()
        conn = _get_conn()
        pid = _pm.get_or_create_project(conn, req.project)
        try:
            pm_backup.log_mutation("INSERT", "pm_node", 0,
                                   new_row={"project": req.project, "kind": req.kind,
                                            "state": req.state, "title": req.title,
                                            "body_preview": req.body[:200]},
                                   metadata={"endpoint": "/project/add_node"})
            node_id = _pm.insert_node(
                conn, pid, req.body, kind=req.kind, state=req.state,
                parent_id=req.parent_id, tags=req.tags, title=req.title,
                trigger_text=req.trigger_text,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        if req.scan_conflicts:
            import threading
            def _bg():
                try:
                    _pm.scan_conflicts(_get_conn(), node_id)
                except Exception as exc:
                    log.warning("pm conflict scan failed for node %s: %s", node_id, exc)
            threading.Thread(target=_bg, daemon=True, name="pm-conflict-scan").start()
        return {"node_id": node_id, "project_id": pid, "queued_conflict_scan": req.scan_conflicts}

    @app.post("/project/tree")
    def pm_tree(req: PMTreeRequest):
        _ensure_pm()
        conn = _get_conn()
        proj = _pm.get_project_by_name(conn, req.project)
        if not proj:
            raise HTTPException(404, f"unknown project: {req.project}")
        return {"project": dict(proj), "nodes": _pm.project_tree(conn, proj["id"])}

    @app.get("/project/node")
    def pm_get_node(node_id: int):
        _ensure_pm()
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, project_id, parent_id, kind, state, title, body, trigger_text, created, updated, "
            "COALESCE(tags, '[]') as tags FROM pm_node WHERE id=?",
            (node_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"node {node_id} not found")
        return dict(row)

    @app.post("/project/subtree")
    def pm_subtree(req: PMSubtreeRequest):
        _ensure_pm()
        conn = _get_conn()
        root = _pm.get_node(conn, req.root_id)
        if not root:
            raise HTTPException(404, f"node {req.root_id} not found")
        project_id = root["project_id"]
        all_nodes = _pm.project_tree(conn, project_id)
        by_parent: dict = {}
        node_map: dict = {}
        for n in all_nodes:
            node_map[n["id"]] = n
            by_parent.setdefault(n.get("parent_id"), []).append(n)
        result = []
        def _walk(nid: int):
            n = node_map.get(nid)
            if n:
                result.append(n)
            for child in by_parent.get(nid, []):
                _walk(child["id"])
        _walk(req.root_id)
        return {"root_id": req.root_id, "nodes": result, "total": len(result)}

    @app.post("/project/status")
    def pm_status(req: PMStatusRequest):
        _ensure_pm()
        conn = _get_conn()
        proj = _pm.get_project_by_name(conn, req.project)
        if not proj:
            raise HTTPException(404, f"unknown project: {req.project}")
        return _pm.project_status(conn, proj["id"], max_bottlenecks=req.max_bottlenecks)

    @app.post("/project/transition")
    def pm_transition(req: PMTransitionRequest):
        _ensure_pm()
        conn = _get_conn()
        node = _pm.get_node(conn, req.node_id)
        if not node:
            return {"ok": False, "error": f"node {req.node_id} not found", "valid_next": []}
        old_state = node["state"]
        valid_next = sorted(_pm.VALID_TRANSITIONS.get(old_state, set()))
        if req.state not in (valid_next if valid_next else []):
            return {"ok": False, "error": f"invalid transition {old_state} -> {req.state}", "valid_next": valid_next}
        try:
            pm_backup.log_mutation("UPDATE", "pm_node", req.node_id,
                                   new_row={"state": req.state},
                                   metadata={"endpoint": "/project/transition"})
            result = _pm.state_transition(conn, req.node_id, req.state)
            return {**result, "ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e), "valid_next": valid_next}

    @app.post("/project/delete_node")
    def pm_delete_node(req: PMDeleteRequest):
        _ensure_pm()
        try:
            pm_backup.log_mutation("DELETE", "pm_node", req.node_id,
                                   metadata={"endpoint": "/project/delete_node",
                                             "expected_title": req.expected_title[:80]})
            result = _pm.delete_node(_get_conn(), req.node_id,
                                     req.confirmation_token, req.expected_title)
            return result
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/project/reparent")
    def pm_reparent(req: PMReparentRequest):
        _ensure_pm()
        try:
            pm_backup.log_mutation("UPDATE", "pm_node", req.node_id,
                                   new_row={"parent_id": req.new_parent_id},
                                   metadata={"endpoint": "/project/reparent"})
            result = _pm.reparent_node(_get_conn(), req.node_id, req.new_parent_id)
            return result
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/project/conflicts")
    def pm_conflicts(req: PMConflictsRequest):
        _ensure_pm()
        conn = _get_conn()
        pid = None
        if req.project:
            proj = _pm.get_project_by_name(conn, req.project)
            if proj:
                pid = proj["id"]
        return {"conflicts": _pm.open_conflicts(conn, pid)}

    @app.post("/project/resolve")
    def pm_resolve(req: PMResolveRequest):
        _ensure_pm()
        ok = _pm.resolve_conflict(_get_conn(), req.conflict_id, req.resolution)
        if not ok:
            raise HTTPException(404, f"unknown conflict id: {req.conflict_id}")
        return {"ok": True}

    @app.post("/project/propose_parent")
    def pm_propose_parent(req: PMProposeParentRequest):
        _ensure_pm()
        conn = _get_conn()
        proj = _pm.get_project_by_name(conn, req.project)
        if not proj:
            return {"candidates": []}
        return {"candidates": _pm.propose_parent(conn, proj["id"], req.body, top_k=req.top_k)}

    @app.post("/project/log_tool_call")
    def pm_log_tool_call(req: PMToolCallLogRequest):
        _ensure_pm()
        conn = _get_conn()
        try:
            log_id = _pm.log_tool_call(
                conn, req.node_id, req.why, req.tool_name,
                tool_args=req.tool_args, result_summary=req.result_summary,
                success=req.success,
            )
            return {"log_id": log_id, "ok": True}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/project/tool_calls/{node_id}")
    def pm_get_tool_calls(node_id: int):
        _ensure_pm()
        conn = _get_conn()
        calls = _pm.get_tool_calls_for_node(conn, node_id)
        return {"calls": calls, "count": len(calls)}

    @app.get("/project/failed_tool_calls/{node_id}")
    def pm_get_failed_tool_calls(node_id: int):
        _ensure_pm()
        conn = _get_conn()
        calls = _pm.get_failed_tool_calls(conn, node_id)
        return {"calls": calls, "count": len(calls)}

    @app.post("/project/orient")
    def pm_orient(req: PMOrientRequest):
        _ensure_pm()
        conn = _get_conn()
        proj = _pm.get_project_by_name(conn, req.project)
        if not proj:
            raise HTTPException(404, f"unknown project: {req.project}")
        pid = proj["id"]
        status = _pm.project_status(conn, pid)
        tree = _pm.project_tree(conn, pid)
        nodes = {n["id"]: n for n in tree}

        modules = {"swiszproj": 42, "swiszmem": 43, "swiszcli": 44, "swiszcode": 52, "hermes": 222}
        mod_counts = {}
        for name, root_id in modules.items():
            if root_id in nodes:
                active = done = 0
                def count_states(nid):
                    nonlocal active, done
                    n = nodes.get(nid)
                    if not n:
                        return
                    s = n["state"]
                    if s == "active": active += 1
                    elif s in ("done", "satisfied"): done += 1
                    for child in [c for c in tree if c.get("parent_id") == nid]:
                        count_states(child["id"])
                count_states(root_id)
                if active > 0:
                    mod_counts[name] = f"{active}a"

        mod_str = " ".join(f"{k}({v})" for k, v in mod_counts.items())
        orphan_ids = [n["id"] for n in tree if n.get("parent_id") and n["parent_id"] not in nodes]
        now = int(time.time())
        stale = [f for f in status.get("frontier", []) if now - f["updated"] > 7 * 86400]
        empty_bodies = [n["id"] for n in tree if n.get("body", "").strip() == "" and n["state"] == "active"]

        real_issues = len(orphan_ids) + len(stale)
        trust = max(0, 100 - real_issues * 5)

        block = f'PM: {req.project} - {status["summary"]} - modules: {mod_str or "none active"} - trust: {trust}%'
        if real_issues:
            block += f' ({len(orphan_ids)} orphans, {len(stale)} stale, {len(empty_bodies)} empty)'

        return {"block": block, "trust": trust,
                "issues": {"orphans": orphan_ids, "stale": len(stale), "empty": empty_bodies}}

    @app.post("/project/health")
    def pm_health_endpoint():
        _ensure_pm()
        conn = _get_conn()
        tree = _pm.project_tree(conn, 1)
        nodes = {n["id"]: n for n in tree}
        orphans = [n for n in tree if n.get("parent_id") and n["parent_id"] not in nodes]
        now = int(time.time())
        stale = [n for n in tree if n["state"] == "active" and now - n["updated"] > 7 * 86400]
        empty = [n for n in tree if (n.get("body") or "").strip() == "" and n["state"] == "active"]
        wrong_state = [n for n in tree if n["kind"] in ("task", "artifact") and n["state"] == "satisfied"]
        critical = len(orphans) + len(stale) + len(wrong_state)
        trust = max(0, 100 - critical * 5)
        return {
            "trust": trust,
            "issues": {
                "orphans": [{"id": n["id"], "title": n["title"][:60], "parent_id": n["parent_id"]} for n in orphans[:5]],
                "stale": len(stale),
                "empty_bodies": len(empty),
                "wrong_state": len(wrong_state),
            }
        }
