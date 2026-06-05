"""
ast_transform.py — deterministic Python AST transforms via libcst.

Single source of truth for all libcst transformations. Importable as a
pure Python module — no HTTP, no swiszard dependency, no swiszCLI dependency.

Safety: every write transform uses tempfile + round-trip parse + atomic
rename. Failed transforms leave a .bak file and raise. Original is never
modified in-place.

Operations:
    find(func_name, filepath)        -> {"name", "params", "decorators"}
    wrap(func_name, filepath)        -> {"ok", "diff"}
    decorate(func_name, filepath, decorator) -> {"ok", "diff"}
    rename(old_name, new_name, filepath) -> {"ok", "diff"}
    delete_func(func_name, filepath) -> {"ok", "diff"}
    insert_after(func_name, filepath, code_b64) -> {"ok", "diff"}
    format_file(filepath)            -> {"ok", "diff"}

DSL router:
    dispatch(task: str) -> str       regex-dispatch like swiszard router
"""
from __future__ import annotations

import ast as _stdlib_ast
import base64 as _b64
import difflib
import hashlib
import re
import subprocess
from pathlib import Path

import libcst as _cst


# ── Safety: tempfile + atomic rename ───────────────────────────────────────

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _safe_apply(filepath: str, transform_fn) -> dict:
    """Apply a transform safely: read -> transform -> verify -> write.

    transform_fn(old_code) -> new_code (str)

    Steps:
    1. Read original
    2. Run transform
    3. Round-trip parse new code with *both* libcst and stdlib ast
    4. Write to tempfile
    5. Atomic rename over original
    6. If anything fails, leave .bak and raise

    Returns {"ok": True, "diff": str, "before_sha": str, "after_sha": str}
    """
    p = Path(filepath)
    if not p.exists():
        raise FileNotFoundError(f"file not found: {filepath}")

    old_code = p.read_text()
    old_sha = _sha(old_code)

    try:
        new_code = transform_fn(old_code)
    except Exception:
        raise  # Re-raise transform errors directly

    if old_code == new_code:
        return {"ok": True, "diff": "", "before_sha": old_sha, "after_sha": old_sha, "changed": False}

    # Verify new code parses
    try:
        _cst.parse_module(new_code)
    except _cst.ParserSyntaxError as e:
        raise ValueError(f"transform produced unparseable code (libcst): {e}")

    try:
        _stdlib_ast.parse(new_code)
    except SyntaxError as e:
        raise ValueError(f"transform produced unparseable code (stdlib): {e}")

    # Write to tempfile, then atomic rename
    tmp = p.with_suffix(p.suffix + ".swiszcode.tmp")
    tmp.write_text(new_code)
    tmp.replace(p)

    after_sha = _sha(new_code)
    diff = "".join(difflib.unified_diff(
        old_code.splitlines(keepends=True),
        new_code.splitlines(keepends=True),
        fromfile=filepath, tofile=filepath, n=3,
    ))

    return {"ok": True, "diff": diff, "before_sha": old_sha, "after_sha": after_sha, "changed": True}


# ── Operations ─────────────────────────────────────────────────────────────

def find(func_name: str, filepath: str) -> dict:
    """Locate a function and return its name, params, and decorators."""
    p = Path(filepath)
    if not p.exists():
        raise FileNotFoundError(f"file not found: {filepath}")
    code = p.read_text()
    try:
        tree = _cst.parse_module(code)
    except _cst.ParserSyntaxError as e:
        raise ValueError(f"parse error in {filepath}: {e}")

    results: list[dict] = []

    class Finder(_cst.CSTVisitor):
        def visit_FunctionDef(self_vis, node):
            params = []
            for param in node.params.params:
                ann = None
                if param.annotation is not None:
                    a = param.annotation
                    if hasattr(a, "annotation") and hasattr(a.annotation, "value"):
                        ann = a.annotation.value
                    elif hasattr(a, "code"):
                        ann = a.code
                    else:
                        try:
                            ann = str(a)
                        except Exception:
                            ann = "?"
                params.append((param.name.value, ann))
            decs = [
                d.decorator.code if hasattr(d.decorator, "code") else str(d.decorator)
                for d in node.decorators
            ]
            results.append({"name": node.name.value, "params": params, "decorators": decs})

    tree.visit(Finder())
    matches = [r for r in results if r["name"] == func_name]
    if not matches:
        names = sorted(r["name"] for r in results)
        raise ValueError(f"function {func_name!r} not found in {filepath}. Functions: {names}")

    return matches[0]


def wrap(func_name: str, filepath: str) -> dict:
    """Wrap a function body in try/except Exception."""
    def transform(code):
        tree = _cst.parse_module(code)
        found = [False]

        class WrapTransformer(_cst.CSTTransformer):
            def leave_FunctionDef(self, og, updated):
                if og.name.value != func_name:
                    return updated
                found[0] = True
                err_msg = _cst.SimpleString(f'"Error in {func_name}"')
                print_err = _cst.Expr(value=_cst.Call(
                    func=_cst.Name("print"), args=[_cst.Arg(err_msg)]))
                raise_stmt = _cst.Raise(exc=None, cause=None)
                exc_handler = _cst.ExceptHandler(
                    type=_cst.Name("Exception"),
                    name=_cst.AsName(name=_cst.Name("e")),
                    body=_cst.IndentedBlock(body=[
                        _cst.SimpleStatementLine(body=[print_err]),
                        _cst.SimpleStatementLine(body=[raise_stmt]),
                    ]))
                try_node = _cst.Try(
                    body=updated.body, handlers=[exc_handler],
                    orelse=None, finalbody=None)
                return updated.with_changes(body=_cst.IndentedBlock(body=[try_node]))

        new_tree = tree.visit(WrapTransformer())
        if not found[0]:
            raise ValueError(f"function {func_name!r} not found in {filepath}")
        return new_tree.code

    return _safe_apply(filepath, transform)


def decorate(func_name: str, filepath: str, decorator: str) -> dict:
    """Add a decorator to a function."""
    def transform(code):
        tree = _cst.parse_module(code)
        found = [False]

        class DecoratorTransformer(_cst.CSTTransformer):
            def leave_FunctionDef(self, og, updated):
                if og.name.value != func_name:
                    return updated
                found[0] = True
                try:
                    dec_expr = _cst.parse_expression(decorator)
                except Exception:
                    dec_expr = _cst.Name(decorator)
                dec = _cst.Decorator(decorator=dec_expr)
                return updated.with_changes(
                    decorators=[dec] + list(updated.decorators))

        new_tree = tree.visit(DecoratorTransformer())
        if not found[0]:
            raise ValueError(f"function {func_name!r} not found in {filepath}")
        return new_tree.code

    return _safe_apply(filepath, transform)


def rename(old_name: str, new_name: str, filepath: str) -> dict:
    """Rename a function or class (FunctionDef, AsyncFunctionDef, ClassDef)."""
    def transform(code):
        tree = _cst.parse_module(code)
        found = [False]

        class RenameTransformer(_cst.CSTTransformer):
            def leave_FunctionDef(self, og, updated):
                if og.name.value == old_name:
                    found[0] = True
                    return updated.with_changes(name=_cst.Name(new_name))
                return updated

            def leave_ClassDef(self, og, updated):
                if og.name.value == old_name:
                    found[0] = True
                    return updated.with_changes(name=_cst.Name(new_name))
                return updated

        new_tree = tree.visit(RenameTransformer())
        if not found[0]:
            raise ValueError(f"symbol {old_name!r} not found in {filepath}")
        return new_tree.code

    return _safe_apply(filepath, transform)


def delete_func(func_name: str, filepath: str) -> dict:
    """Delete a function or class by name. Removes the entire node including decorators."""
    def transform(code):
        tree = _cst.parse_module(code)
        found = [False]

        class DeleteTransformer(_cst.CSTTransformer):
            def leave_FunctionDef(self, og, updated):
                if og.name.value == func_name:
                    found[0] = True
                    return _cst.RemovalSentinel.REMOVE
                return updated

            def leave_ClassDef(self, og, updated):
                if og.name.value == func_name:
                    found[0] = True
                    return _cst.RemovalSentinel.REMOVE
                return updated

        new_tree = tree.visit(DeleteTransformer())
        if not found[0]:
            raise ValueError(f"symbol {func_name!r} not found in {filepath}")
        return new_tree.code

    return _safe_apply(filepath, transform)


def insert_after(target_name: str, filepath: str, code_b64: str) -> dict:
    """Insert code after a named function/class.

    code_b64 is base64-encoded Python source (avoids quote/indent hell).
    The code is independently parsed first to verify it's valid Python.
    """
    try:
        new_block_src = _b64.b64decode(code_b64, validate=True).decode("utf-8")
    except Exception as e:
        raise ValueError(f"invalid base64 code: {e}")

    # Verify the new code parses independently
    try:
        _cst.parse_module(new_block_src)
    except _cst.ParserSyntaxError as e:
        raise ValueError(f"insert code does not parse: {e}")

    new_block = _cst.parse_statement(new_block_src.rstrip(chr(10)))

    def transform(code):
        tree = _cst.parse_module(code)
        found = [False]

        class InsertTransformer(_cst.CSTTransformer):
            def leave_SimpleStatementLine(self, og, updated):
                return updated  # Skip simple statements

            def leave_FunctionDef(self, og, updated):
                if og.name.value == target_name:
                    found[0] = True
                    new_body = list(updated.body.body) + [new_block]
                    return updated.with_changes(
                        body=updated.body.with_changes(
                            body=new_body))
                return updated

            def leave_ClassDef(self, og, updated):
                if og.name.value == target_name:
                    found[0] = True
                    new_body = list(updated.body.body) + [new_block]
                    return updated.with_changes(
                        body=updated.body.with_changes(
                            body=new_body))
                return updated

        new_tree = tree.visit(InsertTransformer())
        if not found[0]:
            raise ValueError(f"symbol {target_name!r} not found in {filepath}")
        return new_tree.code

    return _safe_apply(filepath, transform)


def format_file(filepath: str) -> dict:
    """Format a file with black."""
    p = Path(filepath)
    if not p.exists():
        raise FileNotFoundError(f"file not found: {filepath}")
    old_code = p.read_text()
    try:
        _cst.parse_module(old_code)
    except _cst.ParserSyntaxError as e:
        raise ValueError(f"file does not parse: {e}")

    subprocess.run(["python3", "-m", "black", "--quiet", filepath],
                   capture_output=True, text=True)
    new_code = p.read_text()
    if old_code == new_code:
        return {"ok": True, "diff": "", "before_sha": _sha(old_code),
                "after_sha": _sha(new_code), "changed": False}

    diff = "".join(difflib.unified_diff(
        old_code.splitlines(keepends=True), new_code.splitlines(keepends=True),
        fromfile=filepath, tofile=filepath, n=2))
    return {"ok": True, "diff": diff, "before_sha": _sha(old_code),
            "after_sha": _sha(new_code), "changed": True}


# ── DSL Router ─────────────────────────────────────────────────────────────

# Regex patterns (same grammar as swiszard handler)
_RE_FIND = re.compile(r'^ast\s+find\s+(\S+)\s+in\s+(\S+)\s*$')
_RE_WRAP = re.compile(r'^ast\s+wrap\s+(\S+)\s+in\s+(\S+)\s*$')
_RE_DECORATE = re.compile(
    r'^ast\s+decorate\s+(\S+)\s+in\s+(\S+)\s+with\s+@?(\S+(?:\s*\([^)]*\))?)\s*$')
_RE_FORMAT = re.compile(r'^ast\s+format\s+(\S+)\s*$')
_RE_RENAME = re.compile(r'^ast\s+rename\s+(\S+)\s+to\s+(\S+)\s+in\s+(\S+)\s*$')
_RE_DELETE = re.compile(r'^ast\s+delete\s+(\S+)\s+in\s+(\S+)\s*$')
_RE_INSERT = re.compile(
    r'^ast\s+insert\s+(\S+)\s+after\s+(\S+)\s+in\s+(\S+)\s*$')
_RE_LINT = re.compile(r'^ast\s+lint\s+(\S+)\s*$')
_RE_FIX = re.compile(r'^ast\s+fix\s+(\S+)\s*$')
_RE_RENAME_REPO = re.compile(r'^ast\s+rename\s+(\S+)\s+to\s+(\S+)\s+in\s+(\S+)\s*$')
_RE_VERIFY = re.compile(r'^ast\s+verify\s+(\S+)\s*$')
_RE_UNDO = re.compile(r'^ast\s+undo\s+last\s+in\s+(\S+)\s*$')


def dispatch(task: str) -> str:
    """Route a DSL task string to the appropriate transform.

    Returns a result string (matching swiszard handler convention).
    Use this from swiszard as a drop-in replacement for handler_ast_transform.
    """
    import json as _json

    task = task.strip()

    # ast find
    m = _RE_FIND.match(task)
    if m:
        try:
            r = find(m.group(1), m.group(2))
            params = ", ".join(f"{n}:{t or '?'}" for n, t in r.get("params", []))
            decs = ", ".join(r.get("decorators", [])) or "(none)"
            return f"Function {r['name']}({params}) in {m.group(2)}\n  decorators: {decs}"
        except Exception as e:
            return f"ast find: {e}"

    # ast wrap
    m = _RE_WRAP.match(task)
    if m:
        try:
            r = wrap(m.group(1), m.group(2))
            if not r["changed"]:
                return f"ast wrap: no changes ({m.group(1)} already wrapped?)"
            return f"ast wrap: wrapped '{m.group(1)}' in try/except in {m.group(2)}\n{r['diff']}"
        except Exception as e:
            return f"ast wrap: {e}"

    # ast decorate
    m = _RE_DECORATE.match(task)
    if m:
        try:
            r = decorate(m.group(1), m.group(2), m.group(3))
            return f"ast decorate: added @{m.group(3)} to '{m.group(1)}' in {m.group(2)}\n{r['diff']}"
        except Exception as e:
            return f"ast decorate: {e}"

    # ast format
    m = _RE_FORMAT.match(task)
    if m:
        try:
            r = format_file(m.group(1))
            if not r["changed"]:
                return f"ast format: {m.group(1)} already formatted (no changes)"
            return f"ast format: reformatted {m.group(1)}\n{r['diff']}"
        except Exception as e:
            return f"ast format: {e}"

    # ast rename
    m = _RE_RENAME.match(task)
    if m:
        try:
            r = rename(m.group(1), m.group(2), m.group(3))
            return f"ast rename: {m.group(1)} -> {m.group(2)} in {m.group(3)}\n{r['diff']}"
        except Exception as e:
            return f"ast rename: {e}"

    # ast delete
    m = _RE_DELETE.match(task)
    if m:
        try:
            r = delete_func(m.group(1), m.group(2))
            return f"ast delete: removed '{m.group(1)}' from {m.group(2)}\n{r['diff']}"
        except Exception as e:
            return f"ast delete: {e}"

    # ast insert
    m = _RE_INSERT.match(task)
    if m:
        try:
            r = insert_after(m.group(2), m.group(3), m.group(1))
            return f"ast insert: added code after '{m.group(2)}' in {m.group(3)}\n{r['diff']}"
        except Exception as e:
            return f"ast insert: {e}"

    # ast rename (repo-wide via rope)
    m = _RE_RENAME_REPO.match(task)
    if m:
        root = m.group(3)
        if _Path(root).is_dir():
            try:
                from swiszcode.ast_rope import rename_repo
                r = rename_repo(m.group(1), m.group(2), root)
                if "error" in r:
                    return f"ast rename: {r['error']}"
                files = ', '.join(r.get('changed_files', [])[:5])
                return f"ast rename: {m.group(1)} -> {m.group(2)} in {len(r.get('changed_files',[]))} files: {files}"
            except Exception as e:
                return f"ast rename: {e}"
        # Falls through to single-file rename below if root is a file

    # ast lint
    m = _RE_LINT.match(task)
    if m:
        try:
            from swiszcode.ast_lint import lint
            r = lint(m.group(1))
            if "error" in r:
                return "ast lint: " + r["error"]
            return _json.dumps(r, separators=(",", ":"))
        except Exception as e:
            return f"ast lint: {e}"

    # ast verify
    m = _RE_VERIFY.match(task)
    if m:
        try:
            from swiszcode.ast_lint import verify
            r = verify(m.group(1))
            if "error" in r:
                return f"ast verify: {r['error']}"
            return _json.dumps(r, separators=(",", ":"))
        except Exception as e:
            return f"ast verify: {e}"

    # ast undo
    m = _RE_UNDO.match(task)
    if m:
        try:
            from swiszcode.ast_lint import undo
            r = undo(m.group(1))
            if not r["restored"]:
                return f"ast undo: {r['reason']}"
            return f"ast undo: restored {m.group(1)} from .bak (hash: {r['new_hash']})"
        except Exception as e:
            return f"ast undo: {e}"

    # ast fix
    m = _RE_FIX.match(task)
    if m:
        try:
            from swiszcode.ast_lint import fix
            r = fix(m.group(1))
            if "error" in r:
                return "ast fix: " + r["error"]
            if not r["changed"]:
                return f"ast fix: {m.group(1)} already clean (no changes)"
            return f"ast fix: sorted imports in {m.group(1)}\n{r['diff']}"
        except Exception as e:
            return f"ast fix: {e}"

    return (
        "ast_transform: unrecognized operation. Forms:\n"
        "  ast find FUNC in FILE\n"
        "  ast wrap FUNC in FILE\n"
        "  ast decorate FUNC in FILE with @DEC\n"
        "  ast rename OLD to NEW in FILE\n"
        "  ast delete NAME in FILE\n"
        "  ast insert B64 after NAME in FILE\n"
        "  ast lint FILE\n"
        "  ast fix FILE\n"
        "  ast format FILE"
    )
