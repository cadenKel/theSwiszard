"""Unified AST pipeline: transform -> re-index -> pin-verify -> (optional) visualize.

CLI:
  python pipeline.py <file> <dsl_command>     full pipeline
  python pipeline.py <file> --verify-only     skip transform, just re-index + verify
  python pipeline.py <file> --visualize       full pipeline + open ast3d viewer
"""
from __future__ import annotations
import sys
import argparse
from pathlib import Path


def run_pipeline(
    filepath: str,
    dsl_command: str | None = None,
    verify_only: bool = False,
    visualize: bool = False,
) -> dict:
    """Run the full transform->index->verify pipeline. Returns result dict."""
    from ast_transform import dispatch as _transform
    from ast_store import get_store
    from ast_pin import pin_verify, read_node_tags
    import sqlite3, json

    results: dict = {"file": filepath, "steps": []}

    # Step 1: transform (skip if verify_only)
    if not verify_only and dsl_command:
        t_result = _transform(dsl_command)
        results["steps"].append({"step": "transform", "result": t_result})
        if "error" in t_result.lower():
            results["error"] = t_result
            return results

    # Step 2: re-index
    store = get_store()
    idx = store.index_file(filepath)
    results["steps"].append({"step": "index", "file": filepath, "functions": idx.get("function_count", 0)})

    # Step 3: pin-verify — find all pm_nodes that claim this file
    try:
        from ast_pin import _get_pm_conn
        conn = _get_pm_conn()
        rows = conn.execute(
            "SELECT id FROM pm_node WHERE json_extract(body, '$.file') = ? OR body LIKE ?",
            (filepath, f"%{Path(filepath).name}%")
        ).fetchall()
        node_ids = [r[0] for r in rows]
        verify_results = []
        stale = []
        for nid in node_ids:
            v = pin_verify(nid, store=store)
            verify_results.append({"node_id": nid, "ok": v.get("ok"), "status": v.get("status")})
            if not v.get("ok"):
                stale.append(nid)
        results["steps"].append({"step": "pin_verify", "checked": len(node_ids), "stale": stale})
    except Exception as e:
        results["steps"].append({"step": "pin_verify", "error": str(e)})

    # Step 4: visualize (optional)
    if visualize:
        from server import ast3d
        ast3d([filepath])

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="swiszcode unified pipeline")
    parser.add_argument("file", help="Python file to process")
    parser.add_argument("dsl_command", nargs="?", help="DSL command to apply (e.g. 'find: main in file.py')")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--visualize", action="store_true")
    args = parser.parse_args()

    result = run_pipeline(
        filepath=args.file,
        dsl_command=args.dsl_command,
        verify_only=args.verify_only,
        visualize=args.visualize,
    )
    import json
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
