"""Unified per-turn context assembler.

Gathers 6 sources in parallel via ThreadPoolExecutor and builds the
extra_system block injected before each LLM turn.

Assembly order:
  1. Pinned memories (always-inject, cap 5)
  2. Top-5 semantic memory matches
  3. Top-5 PM nodes (/project/recall_triggers)
  4. Top-5 swiszContext chunks (p0_store)
  5. Code context block
  6. Anticipated context (trajectory prediction, if active)
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

_PM_DEFAULT = "http://127.0.0.1:8765"
_BUDGET = 2000  # chars total per assembled block
_SECTION_CAP = 400  # chars per section before truncation


@dataclass
class AssemblyResult:
    pinned: list[dict] = field(default_factory=list)
    memories: list[dict] = field(default_factory=list)
    pm_nodes: list[dict] = field(default_factory=list)
    ctx_chunks: list[dict] = field(default_factory=list)
    code_hits: list[dict] = field(default_factory=list)
    anticipated: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts: list[str] = []

        def _add(header: str, items: list[dict], key: str = "text") -> None:
            if not items:
                return
            lines = [header]
            total = 0
            for item in items:
                txt = (item.get(key) or item.get("body") or item.get("title") or "")
                txt = txt[:_SECTION_CAP]
                if total + len(txt) > _BUDGET:
                    break
                lines.append(f"  - {txt}")
                total += len(txt)
            parts.append("\n".join(lines))

        _add("<!-- PINNED MEMORIES -->", self.pinned)
        _add("<!-- SEMANTIC MEMORIES -->", self.memories)
        _add("<!-- PM NODES -->", self.pm_nodes, key="trigger_text")
        _add("<!-- CONTEXT CHUNKS -->", self.ctx_chunks, key="text")
        _add("<!-- CODE CONTEXT -->", self.code_hits, key="text")
        _add("<!-- ANTICIPATED CONTEXT -->", self.anticipated)

        block = "\n\n".join(parts)
        return block[:_BUDGET]


class ContextAssembler:
    """Threaded context assembler. Call assemble(query) per turn."""

    def __init__(
        self,
        *,
        mem_client: Any,          # swiszcli.memory.MemoryClient
        embed_fn: Callable[[str], list[float]] | None = None,
        p0_store: Any | None = None,    # context_store.ContextStore
        session_id: str | None = None,
        pm_url: str | None = None,
        pm_project: str = "swiszard",
        code_hit_fn: Callable[[str], list[dict]] | None = None,
    ) -> None:
        self._mem = mem_client
        self._embed = embed_fn
        self._p0 = p0_store
        self._session_id = session_id
        self._pm_url = pm_url or os.environ.get("SWISZCLI_PM_URL", _PM_DEFAULT)
        self._pm_project = pm_project
        self._code_hit_fn = code_hit_fn

    # ── sources ────────────────────────────────────────────────────────────

    def _fetch_memories(self, query: str) -> tuple[list[dict], list[dict]]:
        raw = self._mem.recall_triggers(query, top_k=10)
        pinned = [m for m in raw if m.get("pinned")][:5]
        semantic = [m for m in raw if not m.get("pinned")][:5]
        return pinned, semantic

    def _fetch_pm_nodes(self, query: str) -> list[dict]:
        try:
            r = requests.post(
                f"{self._pm_url}/project/recall_triggers",
                json={"query": query, "active_project": self._pm_project, "top_k": 5},
                timeout=3,
            )
            r.raise_for_status()
            return r.json().get("matches", [])
        except Exception as e:
            raise RuntimeError(f"PM recall_triggers: {e}") from e

    def _fetch_ctx_chunks(self, query: str) -> list[dict]:
        if self._p0 is None or self._embed is None:
            return []
        vec = self._embed(query)
        if vec is None:
            return []
        return self._p0.recall_chunks(
            vec, top_k=5, session_id=self._session_id, min_score=0.55
        )

    def _fetch_code_hits(self, query: str) -> list[dict]:
        if self._code_hit_fn is None:
            return []
        return self._code_hit_fn(query)

    def _fetch_anticipated(
        self, traj: Any | None
    ) -> list[dict]:
        """Layer 3 ANTICIPATE: prefetch via trajectory prediction."""
        if traj is None or self._mem is None:
            return []
        try:
            if traj.is_settled():
                return []
            predicted = traj.predict_next()
            if predicted is None:
                return []
            # Use predicted vector to recall — MemoryClient.recall_triggers expects str.
            # We synthesise a pseudo-query from the trajectory centroid text if available.
            centroid_text = getattr(traj, "centroid_text", None)
            if not centroid_text:
                return []
            hits = self._mem.recall_triggers(centroid_text, top_k=3)
            return [h for h in hits][:2]
        except Exception:
            return []

    # ── public ─────────────────────────────────────────────────────────────

    def assemble(
        self,
        query: str,
        traj: Any | None = None,
        extra_code_hits: list[dict] | None = None,
    ) -> AssemblyResult:
        """Gather all sources in parallel. Never raises — errors land in result.errors."""
        result = AssemblyResult(code_hits=extra_code_hits or [])

        tasks = {
            "memories": lambda: self._fetch_memories(query),
            "pm_nodes": lambda: self._fetch_pm_nodes(query),
            "ctx_chunks": lambda: self._fetch_ctx_chunks(query),
            "anticipated": lambda: self._fetch_anticipated(traj),
        }
        if not extra_code_hits and self._code_hit_fn:
            tasks["code_hits"] = lambda: self._fetch_code_hits(query)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks.items()}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    val = fut.result()
                    if name == "memories":
                        result.pinned, result.memories = val
                    elif name == "pm_nodes":
                        result.pm_nodes = val
                    elif name == "ctx_chunks":
                        result.ctx_chunks = val
                    elif name == "anticipated":
                        result.anticipated = val
                    elif name == "code_hits":
                        result.code_hits = val
                except Exception as e:
                    result.errors.append(f"{name}: {e}")

        # Deduplicate: remove PM nodes whose body text matches a flat memory
        mem_texts = {(m.get("text") or "")[:120] for m in result.pinned + result.memories}
        result.pm_nodes = [
            n for n in result.pm_nodes
            if (n.get("trigger_text") or "")[:120] not in mem_texts
        ]

        return result
