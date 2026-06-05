"""
ast_extract.py — deterministic Python AST extraction (stdlib ast only).

Produces structured node/edge data. No embeddings, no colors, no layout —
those are visualization concerns that live elsewhere. This module is the
single source of truth for "what's in this Python file."
"""
from __future__ import annotations

import ast
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────

# AST node types we care about for structured representation.
# Others are skipped (their children may still be visited).
KEEP: set[str] = {
    "Module", "FunctionDef", "AsyncFunctionDef", "ClassDef",
    "Import", "ImportFrom",
    "For", "While", "If", "Try", "With",
    "Raise", "Return", "Assert",
    "Assign", "AnnAssign", "AugAssign",
    "Call", "Expr",
}

# ── AST Dumper ────────────────────────────────────────────────────────────

class ASTDumper(ast.NodeVisitor):
    """Walk a Python AST and produce a flat list of interesting nodes + edges.

    Nodes get sequential integer IDs. Edges represent parent→child
    relationships within the interesting-node subset (skip-nodes are
    transparent for depth calculation but don't appear in output).
    """

    def __init__(self, lines: list[str] | None = None):
        self.lines = lines or []
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self._stack: list[int | None] = []
        self._seen: set[int] = set()
        self._nid: int = 0

    def visit(self, node: ast.AST) -> None:
        if id(node) in self._seen:
            return
        self._seen.add(id(node))
        tname = type(node).__name__
        if tname not in KEEP:
            super().generic_visit(node)
            return

        idx = self._nid
        self._nid += 1

        # Name
        name = tname
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Import):
            name = "import " + ",".join(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = f"from {node.module or '.'} import ..."

        # Depth (skip-None entries are transparent)
        depth = len([x for x in self._stack if x is not None])

        # Source code snippet
        code = ""
        ln = getattr(node, "lineno", 0)
        eln = getattr(node, "end_lineno", 0) or ln
        if ln and eln and self.lines:
            s, e = ln - 1, min(eln, len(self.lines))
            if 0 <= s < e:
                raw = "".join(self.lines[s:e]).strip()
                code = raw[:500] + ("..." if len(raw) > 500 else "")

        nd: dict = {
            "id": idx,
            "name": name,
            "type": tname,
            "depth": depth,
            "lineno": ln,
            "end_lineno": eln,
            "code": code,
        }
        self.nodes.append(nd)

        # Parent edge
        parent = None
        for p in reversed(self._stack):
            if p is not None:
                parent = p
                break
        if parent is not None:
            self.edges.append({"from": parent, "to": idx})

        self._stack.append(idx)
        super().generic_visit(node)
        self._stack.pop()


# ── Public API ────────────────────────────────────────────────────────────

def extract_ast(filepath: str | Path) -> dict:
    """Parse a Python file and return {file, nodes, edges}.

    nodes: list of dicts with id, name, type, depth, lineno, end_lineno, code
    edges: list of dicts with from (parent node id) and to (child node id)
    """
    p = Path(filepath)
    src = p.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src, filename=str(p))
    dumper = ASTDumper(lines)
    dumper.visit(tree)
    return {"file": str(p), "nodes": dumper.nodes, "edges": dumper.edges}
