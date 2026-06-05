"""ast_lint.py — ruff integration for deterministic Python linting.

pure Python module — no HTTP, no swiszard dependency.
"""

from __future__ import annotations
import json as _json
import subprocess as _sp
from pathlib import Path as _Path


def lint(filepath: str) -> dict:
    """Run ruff check on a file, return violations as structured dict."""
    p = _Path(filepath)
    if not p.exists():
        return {"error": f"file not found: {filepath}"}

    result = _sp.run(
        ["ruff", "check", str(p), "--output-format", "json"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode not in (0, 1):
        return {"error": f"ruff failed: {result.stderr.strip()}"}

    violations = _json.loads(result.stdout) if result.stdout.strip() else []
    return {
        "file": filepath,
        "violations": violations,
        "count": len(violations),
    }


def fix(filepath: str) -> dict:
    """Run ruff --fix --select I on a file, return diff if changed."""
    p = _Path(filepath)
    if not p.exists():
        return {"error": f"file not found: {filepath}"}

    old = p.read_text()

    result = _sp.run(
        ["ruff", "check", str(p), "--fix", "--select", "I"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode not in (0, 1):
        return {"error": f"ruff fix failed: {result.stderr.strip()}"}

    new = p.read_text()
    changed = old != new
    diff = ""
    if changed:
        import difflib
        diff = "".join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=filepath, tofile=filepath, n=3
        ))

    return {
        "file": filepath,
        "changed": changed,
        "diff": diff,
    }
