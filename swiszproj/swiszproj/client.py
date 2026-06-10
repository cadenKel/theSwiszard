"""swiszproj: thin HTTP client for swiszPM routes.

Mirrors memory_server.app /project/* endpoints exactly. Fails loud.
The server accepts project NAMES (strings) for most routes, resolving
to project_id internally.
"""
from __future__ import annotations
from typing import Any
import httpx


class ProjectClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        r = self._client.post(f"{self.base_url}{path}", json=payload)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str) -> Any:
        r = self._client.get(f"{self.base_url}{path}")
        r.raise_for_status()
        return r.json()

    # ---- projects ----
    def create(self, name: str) -> dict[str, Any]:
        return self._post("/project/create", {"name": name})

    def list(self) -> list[dict[str, Any]]:
        data = self._get("/project/list")
        return data.get("projects", []) if isinstance(data, dict) else data

    def tree(self, project: str) -> dict[str, Any]:
        return self._post("/project/tree", {"project": project})

    # ---- nodes ----
    def add_node(self, project: str, body: str, *,
                 kind: str = "objective", state: str = "proposed",
                 parent_id: int | None = None,
                 tags: list[str] | None = None,
                 title: str | None = None,
                 trigger_text: str = "",
                 scan_conflicts: bool = True) -> dict[str, Any]:
        return self._post("/project/add_node", {
            "project": project, "body": body, "kind": kind, "state": state,
            "parent_id": parent_id, "tags": tags or [],
            "title": title, "trigger_text": trigger_text,
            "scan_conflicts": scan_conflicts,
        })

    def propose_parent(self, project: str, body: str,
                       top_k: int = 5) -> list[dict[str, Any]]:
        data = self._post("/project/propose_parent",
                          {"project": project, "body": body, "top_k": top_k})
        return data.get("candidates", []) if isinstance(data, dict) else data

    # ---- tool call log ----
    def log_tool_call(self, node_id: int, why: str, tool_name: str,
                      tool_args: dict | None = None, result_summary: str = "",
                      success: bool = True) -> dict[str, Any]:
        return self._post("/project/log_tool_call", {
            "node_id": node_id, "why": why, "tool_name": tool_name,
            "tool_args": tool_args or {}, "result_summary": result_summary,
            "success": success,
        })

    def get_tool_calls(self, node_id: int) -> list[dict[str, Any]]:
        data = self._get(f"/project/tool_calls/{node_id}")
        return data.get("calls", []) if isinstance(data, dict) else data

    def get_failed_tool_calls(self, node_id: int) -> list[dict[str, Any]]:
        data = self._get(f"/project/failed_tool_calls/{node_id}")
        return data.get("calls", []) if isinstance(data, dict) else data

    # ---- conflicts ----
    def conflicts(self, project: str | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {}
        if project:
            payload["project"] = project
        data = self._post("/project/conflicts", payload)
        return data.get("conflicts", []) if isinstance(data, dict) else data

    def resolve(self, conflict_id: int, resolution: str) -> dict[str, Any]:
        return self._post("/project/resolve",
                          {"conflict_id": conflict_id, "resolution": resolution})

    # ---- state transitions ----
    def transition(self, node_id: int, state: str) -> dict[str, Any]:
        return self._post("/project/transition",
                          {"node_id": node_id, "state": state})

    # ---- project compass ----
    def status(self, project: str, max_bottlenecks: int = 5) -> dict[str, Any]:
        return self._post("/project/status",
                          {"project": project, "max_bottlenecks": max_bottlenecks})
