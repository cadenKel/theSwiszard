"""ast_rope.py — rope integration for repo-aware refactoring.

Pure Python module — no HTTP, no swiszard dependency.
"""
from __future__ import annotations
from pathlib import Path as _Path
import json as _json


def rename_repo(old_name: str, new_name: str, project_root: str) -> dict:
    """Rename a symbol across an entire project using rope.

    Finds all definitions and call sites of old_name across the project
    and renames to new_name. Returns changed files and diffs.
    """
    import rope.base.project
    import rope.refactor.rename

    root = _Path(project_root).resolve()
    if not root.is_dir():
        return {"error": f"not a directory: {project_root}"}

    # Rope needs a .ropeproject folder
    rope_dir = root / ".ropeproject"
    rope_dir.mkdir(exist_ok=True)
    config_path = rope_dir / "config.py"
    if not config_path.exists():
        config_path.write_text("# rope project config\n")

    try:
        project = rope.base.project.Project(str(root))
        # Find all occurrences
        changes = rope.refactor.rename.Rename(project, None, None).get_changes(
            old_name, new_name
        )

        if not changes or not changes.changes:
            return {"error": f"no occurrences of '{old_name}' found in {project_root}"}

        changed_files = []
        diffs = {}

        for change in changes.changes:
            filepath = change.resource.real_path if hasattr(change.resource, 'real_path') else str(change.resource.path)
            if filepath not in changed_files:
                changed_files.append(filepath)

            # Get the diff for this file
            old_text = change.resource.read()
            # Apply via rope's do method
            rope_diffs = change.get_description()
            diffs[filepath] = rope_diffs

        # Actually apply the changes
        project.do(changes)

        return {
            "old_name": old_name,
            "new_name": new_name,
            "project_root": str(root),
            "changed_files": changed_files,
            "diffs": diffs,
        }
    except Exception as e:
        return {"error": f"rope rename failed: {e}"}
    finally:
        try:
            project.close()
        except:
            pass
