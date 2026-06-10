"""Full tool-result archive.

Why: format_tool_result feeds at most ~8000 chars back to the model so the
LLMs context window stays survivable. But the full output must NOT be
silently lost — Sean or a later wizard needs to be able to view it. So
every tool result is written, in full, to an on-disk archive keyed by
(session, seq), and the formatted body includes a pointer ref like
[archive:swisz_abc/3] that the model knows it can request via the
view <ref> slash (or via swiszard once we plumb it).

Storage: <state_dir>/archive/<session_id>/<seq>.txt and a .meta.json.
NO silent drops. Full body always persisted before truncation pointer
appears in the model context.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class ToolArchive:
    def __init__(self, root: Path, session_id: str) -> None:
        self.root = Path(root) / session_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self._seq = 0
        self._lock = threading.Lock()

    def write(self, task: str, result: str, *, trace_id: str | None = None) -> str:
        with self._lock:
            self._seq += 1
            seq = self._seq
        body_path = self.root / f"{seq:06d}.txt"
        meta_path = self.root / f"{seq:06d}.meta.json"
        body_path.write_text(result)
        meta_path.write_text(json.dumps({
            "session_id": self.session_id,
            "seq": seq,
            "task": task,
            "trace_id": trace_id,
            "len": len(result),
            "ts": time.time(),
        }))
        return f"[archive:{self.session_id}/{seq:06d}]"

    def read(self, ref: str) -> str:
        sess, seq = self._parse(ref)
        path = self.root.parent / sess / f"{seq}.txt"
        if not path.exists():
            raise FileNotFoundError(f"no archive entry {ref!r} at {path}")
        return path.read_text()

    def meta(self, ref: str) -> dict[str, Any]:
        sess, seq = self._parse(ref)
        path = self.root.parent / sess / f"{seq}.meta.json"
        if not path.exists():
            raise FileNotFoundError(f"no archive meta {ref!r} at {path}")
        return json.loads(path.read_text())

    @staticmethod
    def _parse(ref: str) -> tuple[str, str]:
        # accept "[archive:sess/seq]" or "archive:sess/seq" or "sess/seq"
        r = ref.strip().lstrip("[").rstrip("]")
        if r.startswith("archive:"):
            r = r[len("archive:"):]
        if "/" not in r:
            raise ValueError(f"bad archive ref {ref!r}")
        sess, seq = r.split("/", 1)
        return sess, seq


# process singleton
_DEFAULT: ToolArchive | None = None


def set_default(arch: ToolArchive) -> None:
    global _DEFAULT
    _DEFAULT = arch


def get_default() -> ToolArchive | None:
    return _DEFAULT
