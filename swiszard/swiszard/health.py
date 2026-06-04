"""
health.py — Swiszard immune system.

Every handler operation reports to a HealthMonitor. Failures are:
  - Written to per-session .errors.jsonl (always survives)
  - Printed to stderr in ANSI red + bell (impossible to miss)
  - Summarized at session end via monitor.summary()

The design principle: no error is silent. If something fails, the
operator knows, even if the LLM doesn't see it.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

# ANSI codes for terminal screaming
RED = "\033[1;31m"
YELLOW = "\033[1;33m"
RESET = "\033[0m"
BELL = "\a"


class HealthEvent:
    __slots__ = ("ts", "event_id", "handler", "ok", "detail", "duration_ms")
    def __init__(self, handler: str, ok: bool, detail: str = "", duration_ms: int = 0):
        self.ts = time.time()
        self.event_id = uuid.uuid4().hex[:8]
        self.handler = handler
        self.ok = ok
        self.detail = detail
        self.duration_ms = duration_ms


class HealthMonitor:
    """Per-session health tracker. Global singleton per swiszard process."""

    def __init__(self, session_id: str = "", errors_dir: str | None = None):
        self.session_id = session_id or f"swisz_{uuid.uuid4().hex[:8]}"
        self.events: list[HealthEvent] = []
        self.error_count = 0
        self.ok_count = 0
        self.errors_dir = Path(errors_dir or os.path.expanduser("~/.swiszard/.errors"))
        self.errors_dir.mkdir(parents=True, exist_ok=True)
        self._errors_path = self.errors_dir / f"{self.session_id}.jsonl"
        self._errors_fh = None
        self._closed = False

    def _ensure_fh(self):
        if self._errors_fh is None and not self._closed:
            try:
                self._errors_fh = open(self._errors_path, "a", encoding="utf-8")
            except Exception:
                # If we can't open the error log, scream to stderr
                print(f"{RED}{BELL}[health] CANNOT OPEN ERROR LOG: {self._errors_path}{RESET}",
                      file=sys.stderr)

    def record(self, handler: str, ok: bool, detail: str = "", duration_ms: int = 0) -> None:
        """Record a health event. If failure, scream."""
        event = HealthEvent(handler, ok, detail, duration_ms)
        self.events.append(event)

        if ok:
            self.ok_count += 1
        else:
            self.error_count += 1
            # SCREAM to stderr — red, bell, impossible to miss
            msg = f"{BELL}{RED}[SWISZARD HEALTH] {handler} FAILED: {detail}{RESET}"
            print(msg, file=sys.stderr, flush=True)

            # Write to per-session error log
            self._ensure_fh()
            if self._errors_fh:
                try:
                    row = {
                        "ts": event.ts,
                        "event_id": event.event_id,
                        "handler": handler,
                        "detail": detail,
                        "duration_ms": duration_ms,
                    }
                    self._errors_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    self._errors_fh.flush()
                except Exception:
                    print(f"{RED}[health] error log write failed{RESET}", file=sys.stderr)

    def summary(self) -> str:
        """Return a one-line health summary. Call at session end."""
        total = self.ok_count + self.error_count
        if self.error_count == 0:
            return f"[swiszard health] {total} ops, 0 errors — clean"
        return (
            f"{RED}[swiszard health] {total} ops, "
            f"{self.error_count} ERRORS — see {self._errors_path}{RESET}"
        )

    def close(self) -> None:
        """Close the error log and print summary."""
        self._closed = True
        if self._errors_fh:
            try:
                self._errors_fh.close()
            except Exception:
                pass
            self._errors_fh = None
        # Print summary to stderr
        print(self.summary(), file=sys.stderr, flush=True)

    def has_errors(self) -> bool:
        return self.error_count > 0


# Module-level singleton — initialized by swiszard_do() on first call
_monitor: Optional[HealthMonitor] = None


def get_monitor() -> HealthMonitor:
    global _monitor
    if _monitor is None:
        _monitor = HealthMonitor()
    return _monitor


def init_monitor(session_id: str = "", errors_dir: str | None = None) -> HealthMonitor:
    global _monitor
    _monitor = HealthMonitor(session_id=session_id, errors_dir=errors_dir)
    return _monitor
