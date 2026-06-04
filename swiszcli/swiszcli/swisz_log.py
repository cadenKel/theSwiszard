"""Per-session log of every swiszard_do() call.

One JSONL file per swiszcli session at:
    <state_dir>/swisz_calls/<session_id>.jsonl

Each line is one swiszard invocation:
{
  "ts": float,                 # unix start time
  "session_id": str,           # parent swiszcli session
  "call_id": str,              # tr_<hex>
  "task": str,                 # full task string passed in
  "task_len": int,
  "handler": str,              # routed handler name, parsed from result
  "result": str,               # full router return string
  "result_len": int,
  "duration_ms": int,
  "error": str|None,           # exception class+msg if raised
}

A symlink <state_dir>/swisz_calls/latest -> current session log.

NO fallbacks. Fail loud if disk write breaks; we log the breakage and
still return the wrapped result so the agent keeps running.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Callable

# Parse router result like:  "handler_shell: ..."  or  "swiszard: no confident handler match. best guess: handler_foo (sim ...)"
_HANDLER_RE = re.compile(r"^(handler_[a-z_]+|swiszard)\b", re.IGNORECASE)
# chain results are JSON arrays — log handler="chain"
_CHAIN_RE = re.compile(r"^\s*\[\s*\{")


def _infer_handler(task: str, result: str) -> str:
    if not isinstance(result, str):
        return "unknown"
    if _CHAIN_RE.match(result):
        return "chain"
    m = _HANDLER_RE.match(result.strip())
    if m:
        return m.group(1).lower()
    # task-side prefix detection
    t = (task or "").lstrip()
    for prefix, name in (
        ("help", "help"),
        ("route:", "route"),
        ("json:", "json_wrap"),
        ("chain:", "chain"),
        ("safety:", "safety"),
        ("feedback:", "feedback"),
        ("memory ", "handler_memory"),
        ("run:", "handler_shell"),
        ("run_b64", "handler_shell"),
        ("run ", "handler_shell"),
        ("read ", "handler_file_read"),
        ("find ", "handler_find"),
        ("grep ", "handler_grep"),
        ("write_b64", "handler_file_write"),
        ("edit ", "handler_edit"),
        ("search the web", "handler_web_search"),
        ("project ", "handler_proj"),
    ):
        if t.startswith(prefix):
            return name
    return "unrouted"


class SwiszCallLog:
    def __init__(self, state_dir: Path, session_id: str) -> None:
        self.session_id = session_id
        self.dir = Path(state_dir) / "swisz_calls"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{session_id}.jsonl"
        # touch + update latest symlink
        self.path.touch()
        latest = self.dir / "latest"
        try:
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(self.path.name)
        except OSError:
            # symlink not critical
            pass
        self._fh = open(self.path, "a", encoding="utf-8")

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def record(self, *, task: str, result: str, duration_ms: int,
               error: str | None = None) -> None:
        row = {
            "ts": time.time(),
            "session_id": self.session_id,
            "call_id": "sw_" + uuid.uuid4().hex[:10],
            "task": task,
            "task_len": len(task) if isinstance(task, str) else -1,
            "handler": _infer_handler(task, result),
            "result": result,
            "result_len": len(result) if isinstance(result, str) else -1,
            "duration_ms": duration_ms,
            "error": error,
        }
        try:
            self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception as e:
            # last-ditch stderr so failures are loud, never silent
            import sys
            print(f"[swisz_log] write failed: {e!r}", file=sys.stderr)


def wrap_swiszard_do(fn: Callable[[str], str], call_log: SwiszCallLog) -> Callable[[str], str]:
    """Return a swiszard_do that records every call into `call_log`."""
    def _wrapped(task: str) -> str:
        t0 = time.perf_counter()
        err = None
        result = ""
        try:
            result = fn(task)
            return result
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            raise
        finally:
            dur = int((time.perf_counter() - t0) * 1000)
            try:
                call_log.record(task=task, result=result if isinstance(result, str) else repr(result),
                                duration_ms=dur, error=err)
            except Exception:
                pass
    return _wrapped


_DEFAULT: SwiszCallLog | None = None


def set_default(log: SwiszCallLog | None) -> None:
    global _DEFAULT
    _DEFAULT = log


def get_default() -> SwiszCallLog | None:
    return _DEFAULT
