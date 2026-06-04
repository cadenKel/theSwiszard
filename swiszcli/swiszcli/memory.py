"""Thin HTTP client for swizmem (shared with Hermes).

Endpoints verified against memory_server/app.py:
  GET  /health
  GET  /status
  POST /remember         {content, triggers, kind, session_id, turn, source, tags}
  POST /recall_triggers  {query, top_k}
  POST /recall_content   {query, top_k, include_deprecated}
  POST /show             {memory_id}
  POST /list             {tag?, source?, include_deprecated, limit, offset}
  POST /pin /unpin /forget   {memory_id}
  POST /deprecate        {memory_id, reason?}
  POST /tag /untag       {memory_id, tag}

All calls fail loud: any non-200 raises. No silent fallbacks.
"""
from __future__ import annotations

from typing import Any

import httpx

from .identity import to_storage as _identity_to_storage, to_render as _identity_to_render
from .source_weights import apply_weights as _apply_source_weights



def _expand_mem(obj):
    """Recursively expand {{user}} sentinels in memory body/text fields."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("body", "content", "text", "lesson", "reason") and isinstance(v, str):
                out[k] = _identity_to_render(v)
            else:
                out[k] = _expand_mem(v)
        return out
    if isinstance(obj, list):
        return [_expand_mem(x) for x in obj]
    return obj


def _expand_and_weight(obj):
    """READ-path: expand sentinels AND apply source weights to recall lists."""
    expanded = _expand_mem(obj)
    if isinstance(expanded, list):
        return _apply_source_weights(expanded)
    return expanded


class MemoryClient:
    def __init__(self, base_url: str, session_id: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
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

    # ── health ────────────────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def status(self) -> dict[str, Any]:
        return self._get("/status")

    # ── write ─────────────────────────────────────────────────────────────
    def remember(
        self,
        content: str,
        *,
        triggers: list[str] | None = None,
        kind: str = "fact",
        turn: int = -1,
        source: str = "swiszcli",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/remember",
            {
                "content": _identity_to_storage(content),
                "triggers": triggers or [],
                "kind": kind,
                "session_id": self.session_id,
                "turn": turn,
                "source": source,
                "tags": tags or [],
            },
        )

    # ── read ──────────────────────────────────────────────────────────────
    def recall_triggers(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        data = self._post("/recall_triggers", {"query": query, "top_k": top_k})
        if isinstance(data, dict):
            data = data.get("results", data.get("memories", []))
        return _expand_and_weight(data)

    def recall_content(self, query: str, top_k: int = 5, include_deprecated: bool = False) -> list[dict[str, Any]]:
        data = self._post(
            "/recall_content",
            {"query": query, "top_k": top_k, "include_deprecated": include_deprecated},
        )
        if isinstance(data, dict):
            data = data.get("results", data.get("memories", []))
        return _expand_and_weight(data)

    def show(self, memory_id: int) -> dict[str, Any]:
        return _expand_mem(self._post("/show", {"memory_id": memory_id}))

    def list_memories(
        self,
        *,
        tag: str | None = None,
        source: str | None = None,
        include_deprecated: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        return _expand_mem(self._post(
            "/list",
            {
                "tag": tag,
                "source": source,
                "include_deprecated": include_deprecated,
                "limit": limit,
                "offset": offset,
            },
        ))

    # ── mutate ────────────────────────────────────────────────────────────
    def pin(self, memory_id: int) -> Any:
        return self._post("/pin", {"memory_id": memory_id})

    def unpin(self, memory_id: int) -> Any:
        return self._post("/unpin", {"memory_id": memory_id})

    def forget(self, memory_id: int) -> Any:
        return self._post("/forget", {"memory_id": memory_id})

    def deprecate(self, memory_id: int, reason: str | None = None) -> Any:
        return self._post("/deprecate", {"memory_id": memory_id, "reason": reason})

    def tag(self, memory_id: int, tag: str) -> Any:
        return self._post("/tag", {"memory_id": memory_id, "tag": tag})

    def untag(self, memory_id: int, tag: str) -> Any:
        return self._post("/untag", {"memory_id": memory_id, "tag": tag})

    def supersede(
        self,
        old_memory_id: int,
        new_content: str,
        *,
        new_triggers: list[str] | None = None,
        lesson: str | None = None,
        tags: list[str] | None = None,
        turn: int = -1,
        source: str = "swiszcli",
    ) -> dict:
        return self._post(
            "/supersede",
            {
                "old_memory_id": old_memory_id,
                "new_content": _identity_to_storage(new_content),
                "new_triggers": new_triggers or [],
                "lesson": lesson,
                "tags": tags or [],
                "session_id": self.session_id,
                "turn": turn,
                "source": source,
            },
        )

    # ── triggers ──────────────────────────────────────────────────────────
    def trigger_list(self, memory_id: int) -> dict:
        return self._post("/trigger_list", {"memory_id": memory_id})

    def trigger_add(self, memory_id: int, trigger_text: str) -> dict:
        return self._post("/trigger_add", {"memory_id": memory_id, "trigger_text": trigger_text})

    def trigger_remove(self, trigger_id: int) -> dict:
        return self._post("/trigger_remove", {"trigger_id": trigger_id})

    # ── code index (project AST chunks) ────────────────────────────────
    def code_index_add(self, root: str) -> dict:
        return self._post("/code/index_add", {"root": root})

    def code_index_remove(self, root: str) -> dict:
        return self._post("/code/index_remove", {"root": root})

    def code_index_list(self) -> dict:
        return self._get("/code/index_list")

    def code_search(self, query: str, top_k: int = 5, repo_id: str | None = None) -> dict:
        payload: dict[str, Any] = {"query": query, "top_k": top_k}
        if repo_id:
            payload["repo_id"] = repo_id
        return self._post("/code/search", payload)
