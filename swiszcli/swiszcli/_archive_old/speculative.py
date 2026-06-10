"""P1.16 speculative prefetch: when a sequence-recipe match is high
confidence AND its first step is a pure read, execute it speculatively
during recall and cache the result. If the model emits that exact
swiszard task this turn, return the cached result instantly — saving a
round-trip per matched recipe.

SAFETY: only pre-fire commands whose task string starts with a
read-only handler verb (no shell, no writes, no memory mutations).
"""
from __future__ import annotations

import re
import time
import threading

# Whitelist: anything matching these prefixes is safe to pre-execute.
SAFE_PREFIXES = (
    "read ",
    "find ",
    "grep ",
    "memory recall ",
    "memory recall+history ",
    "memory show ",
    "memory list",
    "memory status",
    "route:",
)
# Hard deny — even if the above somehow matches, never run these verbs.
DENY_SUBSTRINGS = (
    "write_b64 ", "run `", "rm ", "mv ", "memory forget ",
    "memory remember ", "memory supersede ", "memory deprecate ",
    "memory pin ", "memory unpin ", "memory tag ", "memory untag ",
)


def is_safe(task: str) -> bool:
    if not task:
        return False
    t = task.strip()
    if any(d in t for d in DENY_SUBSTRINGS):
        return False
    return t.startswith(SAFE_PREFIXES)


def _normalize(task: str) -> str:
    """Canonical form for cache key: strip + collapse whitespace + lower."""
    return re.sub(r"\s+", " ", (task or "").strip()).lower()


class SpeculativeCache:
    """Tiny TTL cache of (normalized_task) -> result string.

    Pre-population: call prime() with a task string and a callable that
    runs the swiszard. Lookup: call lookup(task) — returns the cached
    result or None.
    """

    def __init__(self, ttl_seconds: float = 60.0, max_entries: int = 32):
        self.ttl = ttl_seconds
        self.max = max_entries
        self._store = {}  # key -> (result, expiry_ts)
        self._lock = threading.Lock()

    def _evict(self):
        now = time.time()
        with self._lock:
            stale = [k for k, (_, exp) in self._store.items() if exp < now]
            for k in stale:
                self._store.pop(k, None)
            if len(self._store) > self.max:
                # drop oldest
                items = sorted(self._store.items(), key=lambda kv: kv[1][1])
                for k, _ in items[: len(self._store) - self.max]:
                    self._store.pop(k, None)

    def prime(self, task: str, runner) -> bool:
        """Execute `task` via `runner(task)` and cache result. Returns True if cached."""
        if not is_safe(task):
            return False
        key = _normalize(task)
        with self._lock:
            if key in self._store and self._store[key][1] > time.time():
                return True  # already fresh
        try:
            result = runner(task)
        except Exception:
            return False
        if not isinstance(result, str) or not result:
            return False
        with self._lock:
            self._store[key] = (result, time.time() + self.ttl)
        self._evict()
        return True

    def lookup(self, task: str):
        key = _normalize(task)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            result, exp = entry
            if exp < time.time():
                self._store.pop(key, None)
                return None
            return result

    def stats(self):
        return {"size": len(self._store), "ttl": self.ttl, "max": self.max}
