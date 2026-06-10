"""Chunk capture + recall integration for the agent loop."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .context_store import ContextStore
from .embed import embed


@dataclass
class ChunkCapture:
    store: ContextStore
    session_id: str = field(default_factory=lambda: f"sess-{int(time.time())}-{uuid.uuid4().hex[:6]}")
    window_size: int = 8
    _live_turns: list = field(default_factory=list)
    _first_two: list = field(default_factory=list)

    def record_turn(self, role: str, text: str) -> None:
        if not text or not text.strip():
            return
        self._live_turns.append((role, text))
        if len(self._first_two) < 2 and role == "user":
            self._first_two.append((role, text))
        if len(self._live_turns) >= self.window_size:
            self._flush_window()

    def _flush_window(self) -> None:
        if not self._live_turns:
            return
        text = chr(10).join(f"{r}: {t}" for r, t in self._live_turns)
        vec = embed(text[:2000])
        self.store.store_chunk(
            session_id=self.session_id,
            kind="chunk_window",
            text=text,
            embedding=vec,
        )
        self._live_turns.clear()

    def record_tool_result(self, task: str, result: str) -> None:
        snippet = f"task: {task}" + chr(10) + chr(10) + "result:" + chr(10) + result[:1500]
        try:
            vec = embed(snippet)
        except Exception as e:
            print(f"[chunk-capture] embed failed on tool result: {e}", flush=True)
            return
        self.store.store_chunk(
            session_id=self.session_id,
            kind="tool_result",
            text=snippet,
            embedding=vec,
        )

    def close_session(self) -> None:
        self._flush_window()
        last_two = self._live_turns[-2:] if self._live_turns else []
        frame_turns = self._first_two + last_two
        if not frame_turns:
            return
        text = "[SESSION FRAME]" + chr(10) + chr(10).join(f"{r}: {t}" for r, t in frame_turns)
        try:
            vec = embed(text[:2000])
            self.store.store_chunk(
                session_id=self.session_id,
                kind="session_frame",
                text=text,
                embedding=vec,
            )
        except Exception as e:
            print(f"[chunk-capture] session_frame embed failed: {e}", flush=True)


def make_recall_fn(store: ContextStore, capture: ChunkCapture):
    def recall(query: str):
        if not query or not query.strip():
            return []
        try:
            vec = embed(query)
        except Exception as e:
            return [{"_error": f"embed failed: {e}"}]
        return store.recall_chunks(
            vec,
            top_k=5,
            session_id=capture.session_id,
            min_score=0.55,
        )
    return recall


def render_chunks(chunks):
    if not chunks:
        return ""
    errors = [c for c in chunks if "_error" in c]
    real = [c for c in chunks if "_error" not in c]
    parts = []
    if errors:
        err = errors[0]["_error"]
        parts.append(f"<recall-error>{err}</recall-error>")
    if real:
        parts.append("<recalled_context>")
        for c in real:
            kind = c["kind"]
            score = c["score"]
            txt = c["text"][:400]
            parts.append(f"  [{kind} score={score:.2f}] {txt}")
        parts.append("</recalled_context>")
    return chr(10).join(parts)
