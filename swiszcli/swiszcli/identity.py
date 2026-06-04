"""P1.6a identity shim — portability for swiszCLI memory storage.

Storage is user-agnostic: bodies use the sentinel token {{user}} (and
{{user_lower}} for lowercase contexts). Per-installation config holds the
real name. Substitution happens at exactly two chokepoints:

  - to_storage(text)  : called on every memory WRITE before persist
                        (real-name -> sentinel)
  - to_render(text)   : called on every memory READ before injection
                        (sentinel -> real-name)

The sentinel form ({{user}}) was chosen over a bare word like "user" to
avoid false positives on legit phrases such as "user table" or
"end-user auth". Fails loud: if user_name is set but contains the
sentinel substring, raises at config-load time.

User-name config lookup order (first hit wins):
  1. SWISZCLI_USER_NAME env var
  2. ~/.swiszcli/identity.json -> {"user_name": "Sean"}
  3. None  (substitution is a no-op; sentinels render literally)
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

SENTINEL = "{{user}}"
SENTINEL_LOWER = "{{user_lower}}"

_IDENTITY_FILE = Path.home() / ".swiszcli" / "identity.json"


@lru_cache(maxsize=1)
def get_user_name() -> str | None:
    env = os.environ.get("SWISZCLI_USER_NAME")
    if env and env.strip():
        name = env.strip()
    elif _IDENTITY_FILE.is_file():
        try:
            data = json.loads(_IDENTITY_FILE.read_text())
        except Exception as e:
            raise RuntimeError(f"identity.json unreadable: {e}") from e
        name = (data.get("user_name") or "").strip() or None
    else:
        name = None
    if name and ("{{" in name or "}}" in name):
        raise RuntimeError("identity.user_name must not contain sentinel braces")
    return name


def reset_cache() -> None:
    """Test hook: drop cached lookup."""
    get_user_name.cache_clear()


def to_storage(text: str) -> str:
    """WRITE-path: collapse real user name to sentinel for portable storage."""
    if not text:
        return text
    name = get_user_name()
    if not name:
        return text
    # Word-boundary replace, case-insensitive, but preserve lowercase variant
    # by mapping all lowercase occurrences to {{user_lower}} first, then the
    # rest to {{user}}.
    pat_lower = re.compile(rf"\b{re.escape(name.lower())}\b")
    pat_any = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
    out = pat_lower.sub(SENTINEL_LOWER, text)
    out = pat_any.sub(SENTINEL, out)
    return out


def to_render(text: str) -> str:
    """READ-path: expand sentinel to real user name for display/injection."""
    if not text:
        return text
    name = get_user_name()
    if not name:
        return text
    return text.replace(SENTINEL, name).replace(SENTINEL_LOWER, name.lower())
