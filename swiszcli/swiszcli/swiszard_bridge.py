"""In-process bridge to the swiszard router.

We import the `swiszard` Python package directly from a configured
path (default: ~/swiszard-clean). No MCP hop, no subprocess.
Calls are synchronous and return whatever string the router produced.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Callable


class SwiszardUnavailable(RuntimeError):
    pass


def load_swiszard_do(swiszard_path: str) -> Callable[[str], str]:
    """Return the `swiszard_do(str) -> str` router function.

    Raises SwiszardUnavailable with a concrete reason if the
    path is wrong or the router doesn't export what we need.
    """
    root = Path(swiszard_path).resolve()
    if not root.exists():
        raise SwiszardUnavailable(f"swiszard path does not exist: {root}")
    pkg = root / "swiszard"
    if not pkg.is_dir():
        raise SwiszardUnavailable(f"no swiszard/ package under {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        router = importlib.import_module("swiszard.router")
    except Exception as e:
        raise SwiszardUnavailable(f"import swiszard.router failed: {e}") from e
    fn = getattr(router, "swiszard_do", None)
    if fn is None:
        raise SwiszardUnavailable("swiszard.router has no swiszard_do")
    return fn
