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


def verify(project_root: str) -> dict:
    """Run ruff check across a project, diff against previous baseline."""
    import hashlib
    root = _Path(project_root).resolve()
    if not root.is_dir():
        return {"error": f"not a directory: {project_root}"}

    result = _sp.run(
        ["ruff", "check", str(root), "--output-format", "json"],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode not in (0, 1):
        return {"error": f"ruff verify failed: {result.stderr.strip()}"}

    violations = _json.loads(result.stdout) if result.stdout.strip() else []
    current_hash = hashlib.sha256(result.stdout.encode()).hexdigest()[:16]

    baseline_path = _Path("/tmp/swiszcode_lint_baseline.json")
    baseline_hash = None
    if baseline_path.exists():
        try:
            saved = _json.loads(baseline_path.read_text())
            baseline_hash = saved.get("hash")
        except Exception:
            pass

    # Save current as new baseline
    baseline_path.write_text(_json.dumps({
        "hash": current_hash,
        "count": len(violations),
        "timestamp": __import__("time").time(),
    }))

    new_violations = violations  # always show all violations
    return {
        "project": str(root),
        "violations": new_violations[:20],  # cap at 20 to avoid bloat
        "count": len(new_violations),
        "hash": current_hash,
        "previous_hash": baseline_hash,
    }


def undo(filepath: str) -> dict:
    """Restore .bak file from last failed transform."""
    import hashlib
    p = _Path(filepath)
    bak = p.with_suffix(p.suffix + ".bak")
    
    if not bak.exists():
        return {"restored": False, "reason": f"no .bak file found: {bak}"}
    
    old_hash = hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else None
    p.write_bytes(bak.read_bytes())
    bak.unlink()
    new_hash = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    
    return {
        "restored": True,
        "file": str(p),
        "old_hash": old_hash,
        "new_hash": new_hash,
    }
