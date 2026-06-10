#!/usr/bin/env python3
"""
MCP server: Python AST/CST transformation tools.

Safety net: every .py write goes through ast.parse → black → ruff.
Tools: read, write, patch, create_file, find, transform, rename, check, tree, ls, shell, grep
"""

import ast
import difflib
import json
import os
import sqlite3
import subprocess
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import libcst as cst
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("python-transformer")

# Absolute paths to venv binaries — repo-root venv
_VENV_BIN = Path(__file__).parent.parent / ".venv" / "bin"
_BLACK = str(_VENV_BIN / "black")
_RUFF = str(_VENV_BIN / "ruff")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_py(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if p.suffix != ".py":
        raise ValueError(f"Only .py files are supported, got: {path}")
    return p


def _resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def _guard(source: str, path: str | None = None) -> tuple[bool, str, str, str]:
    """ast.parse → black → ruff --fix. Returns (ok, formatted_source, error, autocorrect_summary)."""
    try:
        ast.parse(source)
    except SyntaxError as e:
        return False, source, f"SyntaxError: {e}", ""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        tmp = Path(f.name)

    try:
        r = subprocess.run(
            [_BLACK, "--quiet", str(tmp)], capture_output=True, text=True
        )
        if r.returncode != 0:
            return False, source, f"black: {r.stderr.strip()}", ""

        rr = subprocess.run(
            [_RUFF, "check", "--fix", "--quiet", str(tmp)],
            capture_output=True,
            text=True,
        )
        fixed = tmp.read_text()
        summary = rr.stderr.strip() or ("black reformatted" if fixed != source else "")
        return True, fixed, "", summary
    finally:
        tmp.unlink(missing_ok=True)


def _write_text(path: Path, content: str) -> tuple[bool, str]:
    """Write a text file, guarding Python files.

    Returns (ok, message).
    """
    if path.suffix == ".py":
        ok, formatted, err, note = _guard(content, str(path))
        if not ok:
            return False, f"REJECTED: {err}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(formatted)
        suffix = f" (auto-corrected: {note})" if note else ""
        return True, f"OK: wrote {len(formatted)} bytes to {path}{suffix}"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True, f"OK: wrote {len(content)} bytes to {path}"


def _parse_json_spec(raw: str | dict | None, tool_name: str) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"{tool_name} expects a JSON object or JSON object string")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{tool_name} expects a JSON object string: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{tool_name} expects a JSON object or JSON object string")
    return data


def _apply_text_operations(source: str, operations: list[dict]) -> str:
    result = source
    for index, op in enumerate(operations, 1):
        kind = op.get("op")
        if kind == "replace":
            old = op["old"]
            new = op["new"]
            count = int(op.get("count", 1))
            if old not in result:
                raise ValueError(f"operation {index}: replace old text not found")
            result = result.replace(old, new, count)
        elif kind == "delete":
            old = op["old"]
            count = int(op.get("count", 1))
            if old not in result:
                raise ValueError(f"operation {index}: delete text not found")
            result = result.replace(old, "", count)
        elif kind == "insert_after":
            anchor = op["anchor"]
            text = op["text"]
            if anchor not in result:
                raise ValueError(f"operation {index}: insert_after anchor not found")
            result = result.replace(anchor, anchor + text, 1)
        elif kind == "insert_before":
            anchor = op["anchor"]
            text = op["text"]
            if anchor not in result:
                raise ValueError(f"operation {index}: insert_before anchor not found")
            result = result.replace(anchor, text + anchor, 1)
        elif kind == "append":
            result = result + op["text"]
        elif kind == "prepend":
            result = op["text"] + result
        else:
            raise ValueError(f"operation {index}: unsupported op '{kind}'")
    return result


def _template_python_package(name: str, description: str) -> tuple[list[str], dict[str, str]]:
    pkg = name.replace("-", "_")
    return (
        ["src", "tests"],
        {
            "pyproject.toml": f"""[build-system]
requires = [\"setuptools>=68\"]
build-backend = \"setuptools.build_meta\"

[project]
name = \"{name}\"
version = \"0.1.0\"
description = \"{description}\"
requires-python = \">=3.12\"

[tool.pytest.ini_options]
pythonpath = [\"src\"]
""",
            f"src/{pkg}/__init__.py": "__all__ = []\n",
            f"src/{pkg}/__main__.py": (
                "def main() -> None:\n"
                f"    print(\"{name} ready\")\n\n"
                "if __name__ == \"__main__\":\n"
                "    main()\n"
            ),
            "tests/test_smoke.py": (
                f"from {pkg}.__main__ import main\n\n"
                "def test_import() -> None:\n"
                "    assert callable(main)\n"
            ),
            "README.md": f"# {name}\n\n{description}\n",
        },
    )


def _template_python_cli(name: str, description: str) -> tuple[list[str], dict[str, str]]:
    return _template_python_package(name, description)


def _template_mcp_server(name: str, description: str) -> tuple[list[str], dict[str, str]]:
    module = name.replace("-", "_")
    return (
        [module],
        {
            "requirements.txt": "mcp[cli]\n",
            f"{module}/server.py": (
                "from mcp.server.fastmcp import FastMCP\n\n"
                f"mcp = FastMCP(\"{name}\")\n\n"
                "@mcp.tool()\n"
                "def ping() -> str:\n"
                "    return \"pong\"\n\n"
                "if __name__ == \"__main__\":\n"
                "    mcp.run()\n"
            ),
            "README.md": f"# {name}\n\n{description}\n",
        },
    )


_VERIFY_PROFILE_DIR = Path(__file__).parent.parent / ".localexp" / "verify_profiles"
_PATH_INDEX_DB = Path(__file__).parent.parent / ".localexp" / "path_index.db"


def _path_index_con() -> sqlite3.Connection:
    _PATH_INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_PATH_INDEX_DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS path_indexes (
            name TEXT PRIMARY KEY,
            root TEXT NOT NULL,
            built_at TEXT NOT NULL,
            entry_count INTEGER NOT NULL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS path_entries (
            index_name TEXT NOT NULL,
            path TEXT NOT NULL,
            basename TEXT NOT NULL,
            lower_path TEXT NOT NULL,
            kind TEXT NOT NULL
        )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_path_entries_name ON path_entries(index_name)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_path_entries_basename ON path_entries(index_name, basename)"
    )
    con.commit()
    return con


def _rank_path_match(path_str: str, query: str) -> tuple[int, int, str]:
    base = Path(path_str).name.lower()
    q = query.lower()
    path_lower = path_str.lower()
    exact_base = 0 if base == q else 1
    contains_base = 0 if q in base else 1
    starts_path = 0 if path_lower.startswith(q) else 1
    contains_path = 0 if q in path_lower else 1
    return (exact_base, contains_base + starts_path + contains_path, path_str)


def _best_index_name(root_path: Path) -> str | None:
    root_text = str(root_path)
    with _path_index_con() as con:
        rows = con.execute("SELECT name, root FROM path_indexes").fetchall()
    matches = [(name, root) for name, root in rows if root_text.startswith(root)]
    if not matches:
        return None
    matches.sort(key=lambda item: len(item[1]), reverse=True)
    return matches[0][0]


def _search_index(query: str, index_name: str, root: Path, limit: int, kind: str) -> list[str]:
    q = query.lower()
    like = f"%{q}%"
    params: list[str] = [index_name, like, like]
    sql = (
        "SELECT path, kind FROM path_entries "
        "WHERE index_name = ? AND (lower_path LIKE ? OR basename LIKE ?)"
    )
    if kind != "any":
        sql += " AND kind = ?"
        params.append(kind)
    with _path_index_con() as con:
        rows = con.execute(sql, params).fetchall()
    root_text = str(root)
    candidates = [path for path, entry_kind in rows if path.startswith(root_text)]
    return sorted(candidates, key=lambda item: _rank_path_match(item, query))[:limit]


def _parse_statement(statement: str) -> cst.BaseStatement:
    module = cst.parse_module(statement.rstrip() + "\n")
    return module.body[0]


def _parse_function_body(body: str) -> tuple[cst.BaseStatement, ...]:
    wrapped = cst.parse_module(
        "def _tmp():\n" + textwrap.indent(body.rstrip() + "\n", "    ")
    )
    fn = wrapped.body[0]
    return tuple(fn.body.body)


def _parse_class_body(body: str) -> tuple[cst.BaseStatement, ...]:
    wrapped = cst.parse_module(
        "class _Tmp:\n" + textwrap.indent((body or "pass").rstrip() + "\n", "    ")
    )
    cls = wrapped.body[0]
    return tuple(cls.body.body)


def _find_top_level_index(body: list[cst.CSTNode], name: str, kind: type) -> int:
    for index, node in enumerate(body):
        if isinstance(node, kind) and getattr(node.name, "value", None) == name:
            return index
    raise ValueError(f"top-level {kind.__name__} '{name}' not found")


def _find_class_index(body: list[cst.CSTNode], name: str) -> int:
    return _find_top_level_index(body, name, cst.ClassDef)


def _find_method_index(class_body: list[cst.CSTNode], name: str) -> int:
    for index, node in enumerate(class_body):
        if isinstance(node, cst.FunctionDef) and getattr(node.name, "value", None) == name:
            return index
    raise ValueError(f"method '{name}' not found")


def _decorator_node(expr: str) -> cst.Decorator:
    return cst.Decorator(decorator=cst.parse_expression(expr))


def _parse_decorator_block(code: str) -> list[cst.Decorator]:
    module = cst.parse_module(code.rstrip() + "\n")
    node = module.body[0]
    if isinstance(node, (cst.FunctionDef, cst.ClassDef)):
        return list(node.decorators)
    raise ValueError("decorator block must parse to a function or class definition")


def _set_or_insert_assign(block: list[cst.CSTNode], name: str, value: str) -> list[cst.CSTNode]:
    stmt = _parse_statement(f"{name} = {value}")
    for idx, node in enumerate(block):
        if isinstance(node, cst.SimpleStatementLine) and node.body:
            small = node.body[0]
            if isinstance(small, cst.Assign):
                target = small.targets[0].target
                if isinstance(target, cst.Name) and target.value == name:
                    block[idx] = stmt
                    return block
    return [stmt] + block


def _remove_import_from_body(body: list[cst.CSTNode], module: str, name: str | None) -> list[cst.CSTNode]:
    new_body: list[cst.CSTNode] = []
    changed = False
    for node in body:
        if isinstance(node, cst.SimpleStatementLine) and node.body:
            stmt = node.body[0]
            if isinstance(stmt, cst.Import) and name is None:
                kept = []
                for alias in stmt.names:
                    imported = alias.name.value if isinstance(alias.name, cst.Name) else alias.name.code
                    if imported != module:
                        kept.append(alias)
                if len(kept) != len(stmt.names):
                    changed = True
                    if kept:
                        new_body.append(node.with_changes(body=[stmt.with_changes(names=kept)]))
                    continue
            if isinstance(stmt, cst.ImportFrom):
                mod = stmt.module.code if stmt.module else ""
                if mod == module:
                    if name is None:
                        changed = True
                        continue
                    if isinstance(stmt.names, list):
                        kept = []
                        for alias in stmt.names:
                            imported = alias.name.value if isinstance(alias.name, cst.Name) else alias.name.code
                            if imported != name:
                                kept.append(alias)
                        if len(kept) != len(stmt.names):
                            changed = True
                            if kept:
                                new_body.append(node.with_changes(body=[stmt.with_changes(names=kept)]))
                            continue
        new_body.append(node)
    if not changed:
        raise ValueError("import to remove not found")
    return new_body


def _insert_import(body: list[cst.CSTNode], statement: cst.BaseStatement) -> list[cst.CSTNode]:
    insert_at = 0
    for idx, node in enumerate(body):
        if isinstance(node, cst.SimpleStatementLine) and node.body and isinstance(
            node.body[0], (cst.Import, cst.ImportFrom)
        ):
            insert_at = idx + 1
    return body[:insert_at] + [statement] + body[insert_at:]


def _edit_python_source(source: str, operations: list[dict]) -> str:
    module = cst.parse_module(source)
    body = list(module.body)

    for index, op in enumerate(operations, 1):
        kind = op.get("op")

        if kind == "add_import":
            module_name = op["module"]
            import_name = op.get("name")
            alias = op.get("alias")
            if import_name:
                stmt = f"from {module_name} import {import_name}"
                if alias:
                    stmt += f" as {alias}"
            else:
                stmt = f"import {module_name}"
                if alias:
                    stmt += f" as {alias}"
            body = _insert_import(body, _parse_statement(stmt))

        elif kind == "remove_import":
            body = _remove_import_from_body(body, op["module"], op.get("name"))

        elif kind == "add_function":
            name = op["name"]
            signature = op.get("signature", "")
            body_text = op.get("body", "pass")
            fn_src = f"def {name}({signature}):\n" + textwrap.indent(body_text.rstrip() + "\n", "    ")
            body.append(cst.parse_module(fn_src).body[0])

        elif kind == "replace_function":
            name = op["name"]
            code = op["code"]
            idx = _find_top_level_index(body, name, cst.FunctionDef)
            body[idx] = cst.parse_module(code.rstrip() + "\n").body[0]

        elif kind == "replace_function_body":
            name = op["name"]
            idx = _find_top_level_index(body, name, cst.FunctionDef)
            fn = body[idx]
            new_body = cst.IndentedBlock(body=_parse_function_body(op["body"]))
            body[idx] = fn.with_changes(body=new_body)

        elif kind == "delete_function":
            name = op["name"]
            idx = _find_top_level_index(body, name, cst.FunctionDef)
            del body[idx]

        elif kind == "add_class":
            name = op["name"]
            body_text = op.get("body", "pass")
            cls_src = f"class {name}:\n" + textwrap.indent(body_text.rstrip() + "\n", "    ")
            body.append(cst.parse_module(cls_src).body[0])

        elif kind == "add_method":
            class_name = op["class"]
            method_name = op["name"]
            signature = op.get("signature", "self")
            body_text = op.get("body", "pass")
            cls_idx = _find_class_index(body, class_name)
            cls = body[cls_idx]
            class_body = list(cls.body.body)
            method_src = f"def {method_name}({signature}):\n" + textwrap.indent(body_text.rstrip() + "\n", "    ")
            class_body.append(cst.parse_module(method_src).body[0])
            body[cls_idx] = cls.with_changes(body=cst.IndentedBlock(body=tuple(class_body)))

        elif kind == "replace_method_body":
            class_name = op["class"]
            method_name = op["name"]
            cls_idx = _find_class_index(body, class_name)
            cls = body[cls_idx]
            class_body = list(cls.body.body)
            method_idx = _find_method_index(class_body, method_name)
            method = class_body[method_idx]
            class_body[method_idx] = method.with_changes(body=cst.IndentedBlock(body=_parse_function_body(op["body"])))
            body[cls_idx] = cls.with_changes(body=cst.IndentedBlock(body=tuple(class_body)))

        elif kind == "delete_method":
            class_name = op["class"]
            method_name = op["name"]
            cls_idx = _find_class_index(body, class_name)
            cls = body[cls_idx]
            class_body = list(cls.body.body)
            method_idx = _find_method_index(class_body, method_name)
            del class_body[method_idx]
            if not class_body:
                class_body = list(_parse_class_body("pass"))
            body[cls_idx] = cls.with_changes(body=cst.IndentedBlock(body=tuple(class_body)))

        elif kind == "set_class_attribute":
            class_name = op["class"]
            attr_name = op["name"]
            value = op["value"]
            cls_idx = _find_class_index(body, class_name)
            cls = body[cls_idx]
            class_body = _set_or_insert_assign(list(cls.body.body), attr_name, value)
            body[cls_idx] = cls.with_changes(body=cst.IndentedBlock(body=tuple(class_body)))

        elif kind == "add_decorator":
            target_name = op["name"]
            target_kind = op.get("target", "function")
            decorator = _decorator_node(op["decorator"])
            if target_kind == "function":
                idx = _find_top_level_index(body, target_name, cst.FunctionDef)
                fn = body[idx]
                body[idx] = fn.with_changes(decorators=[*fn.decorators, decorator])
            elif target_kind == "class":
                idx = _find_class_index(body, target_name)
                cls = body[idx]
                body[idx] = cls.with_changes(decorators=[*cls.decorators, decorator])
            elif target_kind == "method":
                class_name = op["class"]
                cls_idx = _find_class_index(body, class_name)
                cls = body[cls_idx]
                class_body = list(cls.body.body)
                method_idx = _find_method_index(class_body, target_name)
                fn = class_body[method_idx]
                class_body[method_idx] = fn.with_changes(decorators=[*fn.decorators, decorator])
                body[cls_idx] = cls.with_changes(body=cst.IndentedBlock(body=tuple(class_body)))
            else:
                raise ValueError(f"operation {index}: unsupported decorator target '{target_kind}'")

        elif kind == "replace_decorators":
            target_name = op["name"]
            target_kind = op.get("target", "function")
            decorators = [_decorator_node(expr) for expr in op.get("decorators", [])]
            if target_kind == "function":
                idx = _find_top_level_index(body, target_name, cst.FunctionDef)
                fn = body[idx]
                body[idx] = fn.with_changes(decorators=decorators)
            elif target_kind == "class":
                idx = _find_class_index(body, target_name)
                cls = body[idx]
                body[idx] = cls.with_changes(decorators=decorators)
            elif target_kind == "method":
                class_name = op["class"]
                cls_idx = _find_class_index(body, class_name)
                cls = body[cls_idx]
                class_body = list(cls.body.body)
                method_idx = _find_method_index(class_body, target_name)
                fn = class_body[method_idx]
                class_body[method_idx] = fn.with_changes(decorators=decorators)
                body[cls_idx] = cls.with_changes(body=cst.IndentedBlock(body=tuple(class_body)))
            else:
                raise ValueError(f"operation {index}: unsupported decorator target '{target_kind}'")

        elif kind == "replace_class_body":
            name = op["name"]
            idx = _find_top_level_index(body, name, cst.ClassDef)
            cls = body[idx]
            new_body = cst.IndentedBlock(body=_parse_class_body(op["body"]))
            body[idx] = cls.with_changes(body=new_body)

        elif kind == "set_constant":
            name = op["name"]
            value = op["value"]
            stmt = _parse_statement(f"{name} = {value}")
            replaced = False
            for idx, node in enumerate(body):
                if isinstance(node, cst.SimpleStatementLine) and node.body:
                    small = node.body[0]
                    if isinstance(small, cst.Assign):
                        target = small.targets[0].target
                        if isinstance(target, cst.Name) and target.value == name:
                            body[idx] = stmt
                            replaced = True
                            break
            if not replaced:
                body.insert(0, stmt)

        elif kind == "append_code":
            extra = cst.parse_module(op["code"].rstrip() + "\n")
            body.extend(extra.body)

        else:
            raise ValueError(f"operation {index}: unsupported python op '{kind}'")

    return module.with_changes(body=tuple(body)).code


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def read(path: str) -> str:
    """Read any text file and return its contents."""
    return _resolve(path).read_text(errors="replace")


@mcp.tool()
def write(path: str, source: str) -> str:
    """Write a .py file. Auto-runs ast.parse → black → ruff --fix before writing.
    Rejected if syntactically invalid. Returns what was auto-corrected.
    """
    p = _resolve_py(path)
    ok, formatted, err, note = _guard(source, path)
    if not ok:
        return f"REJECTED: {err}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(formatted)
    suffix = f" (auto-corrected: {note})" if note else ""
    return f"OK: wrote {len(formatted)} bytes to {p}{suffix}"


@mcp.tool()
def transform(path: str, transformer_code: str) -> str:
    """Apply a libcst CSTTransformer to a file.

    transformer_code must define a class named ``Transform`` that inherits from
    ``libcst.CSTTransformer``. The full ``libcst`` (as ``cst``) and ``libcst.matchers``
    (as ``m``) namespaces are available.

    After transformation the result is validated and formatted before writing.

    Example transformer_code:
        class Transform(cst.CSTTransformer):
            def leave_Name(self, original_node, updated_node):
                if original_node.value == "foo":
                    return updated_node.with_changes(value="bar")
                return updated_node
    """
    p = _resolve_py(path)
    source = p.read_text()

    ns: dict = {
        "cst": cst,
        "m": __import__("libcst.matchers", fromlist=["*"]),
        "__builtins__": __builtins__,
    }
    try:
        exec(compile(transformer_code, "<transformer>", "exec"), ns)
    except Exception as e:
        return f"COMPILE_ERROR: {e}"

    TransformClass = ns.get("Transform")
    if TransformClass is None:
        return "ERROR: transformer_code must define a class named 'Transform'"

    try:
        module = cst.parse_module(source)
        new_module = module.visit(TransformClass())
        new_source = new_module.code
    except Exception as e:
        return f"TRANSFORM_ERROR: {e}"

    ok, formatted, err, note = _guard(new_source, path)
    if not ok:
        return f"POST_TRANSFORM_REJECTED: {err}\n\nRaw output:\n{new_source[:2000]}"

    p.write_text(formatted)
    suffix = f" (auto-corrected: {note})" if note else ""
    return f"OK: transformation applied, {len(formatted)} bytes written to {p}{suffix}"


@mcp.tool()
def rename(path: str, line: int, col: int, new_name: str) -> str:
    """Rename the symbol at (line, col) using rope.

    line is 1-based, col is 0-based character offset within that line.
    After renaming the result is formatted (black + ruff).
    """
    try:
        import rope.base.project as rproject
        import rope.refactor.rename as rrename
    except ImportError:
        return "ERROR: rope is not installed — pip install rope"

    p = _resolve_py(path)
    proj = rproject.Project(str(p.parent))
    try:
        resource = proj.get_resource(p.name)
        lines = p.read_text().splitlines(keepends=True)
        if line < 1 or line > len(lines):
            return f"ERROR: line {line} out of range (file has {len(lines)} lines)"
        offset = sum(len(ln) for ln in lines[: line - 1]) + col
        changes = rrename.Rename(proj, resource, offset).get_changes(new_name)
        proj.do(changes)

        new_source = p.read_text()
        ok, formatted, _, note = _guard(new_source, path)
        if ok:
            p.write_text(formatted)
        suffix = f" (auto-corrected: {note})" if note else ""
        return f"OK: renamed symbol to '{new_name}'{suffix}"
    except Exception as e:
        return f"ROPE_ERROR: {e}"
    finally:
        proj.close()


@mcp.tool()
def check(path: str) -> str:
    """Show unfixable lint errors in a .py file.

    Runs ruff --fix first (auto-corrects everything fixable in-place), then
    reports only what remains. If the file is clean returns 'OK: no issues'.
    You should only see errors here that require manual thought to resolve.
    """
    p = _resolve_py(path)
    # Auto-fix first
    subprocess.run([_RUFF, "check", "--fix", "--quiet", str(p)], capture_output=True)
    subprocess.run([_BLACK, "--quiet", str(p)], capture_output=True)
    # Now report what's left
    r = subprocess.run(
        [_RUFF, "check", "--output-format=json", str(p)],
        capture_output=True,
        text=True,
    )
    try:
        issues = json.loads(r.stdout)
        if not issues:
            return "OK: no issues"
        # Return compact form — just code, message, line
        compact = [
            {"code": i["code"], "line": i["location"]["row"], "msg": i["message"]}
            for i in issues
        ]
        return json.dumps(compact, indent=2)
    except Exception:
        return r.stdout or r.stderr or "OK: no issues"


@mcp.tool()
def tree(path: str, mode: str = "ast") -> str:
    """Dump the structure of a Python file for inspection.

    mode:
      "ast"  — ast.dump with indentation (default, readable)
      "cst"  — libcst Module repr (verbose, complete; truncated at 12 000 chars)
    """
    p = _resolve_py(path)
    source = p.read_text()

    if mode == "cst":
        try:
            module = cst.parse_module(source)
            result = repr(module)
            if len(result) > 12000:
                result = result[:12000] + "\n...(truncated)"
            return result
        except Exception as e:
            return f"CST_ERROR: {e}"
    else:
        try:
            return ast.dump(ast.parse(source), indent=2)
        except SyntaxError as e:
            return f"SyntaxError: {e}"


@mcp.tool()
def ls(path: str = ".") -> str:
    """List directory contents. Returns name, type (file/dir), and size."""
    p = _resolve(path)
    if not p.exists():
        return f"ERROR: {path} does not exist"
    if p.is_file():
        return f"{p.name}  {p.stat().st_size}B"
    lines = []
    for child in sorted(p.iterdir()):
        kind = "dir" if child.is_dir() else "file"
        size = f"  {child.stat().st_size}B" if child.is_file() else ""
        lines.append(f"{kind}  {child.name}{size}")
    return "\n".join(lines) or "(empty)"


@mcp.tool()
def shell(cmd: str, cwd: str | None = None) -> str:
    """Run a shell command. cwd defaults to the MCP server directory.

    stdout and stderr are returned. Timeout: 30s.
    Commands run with the venv's bin on PATH so ruff/black/python are available.
    """
    env_path = str(_VENV_BIN) + ":" + __import__("os").environ.get("PATH", "")
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd or str(Path(__file__).parent),
            env={**__import__("os").environ, "PATH": env_path},
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr] {err}")
        parts.append(f"[exit {r.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return "[timeout after 30s]"


@mcp.tool()
def searxng(query: str, timeout: int = 10) -> dict:
    """Query the SearXNG instance.

    Sends an HTTP GET request to http://<host>:8080/search?q={query}&format=json
    Returns parsed JSON response with results.

    The host is detected automatically from docker network (default: 172.18.0.3).
    """
    import requests

    # Try to detect the SearXNG instance - use localhost if container not found on network
    try:
        r = requests.get(
            "http://localhost:8080/search",
            params={"q": query, "format": "json", "safesearch": 1},
            timeout=timeout,
            allow_redirects=True,
        )
        if r.status_code == 200:
            return {"status": "ok", "results": r.json().get("results", [])}
    except Exception:
        pass

    # Fall back to docker network IP
    try:
        r = requests.get(
            "http://172.18.0.3:8080/search",
            params={"q": query, "format": "json", "safesearch": 1},
            timeout=timeout,
            allow_redirects=True,
        )
        if r.status_code == 200:
            return {"status": "ok", "results": r.json().get("results", [])}
    except Exception:
        pass

    # Try localhost again with different port
    try:
        for port in [80, 443, 8080, 5000]:
            r = requests.get(
                f"http://localhost:{port}/search",
                params={"q": query, "format": "json", "safesearch": 1},
                timeout=timeout,
                allow_redirects=True,
            )
            if r.status_code == 200:
                return {"status": "ok", "results": r.json().get("results", [])}
    except Exception:
        pass

    return {"status": "error", "message": "Could not connect to SearXNG on any host"}


@mcp.tool()
def grep(
    pattern: str | None = None,
    path: str = ".",
    recursive: bool = False,
    query: str | None = None,
    text: str | None = None,
    root: str | None = None,
    glob: str | None = None,
) -> str:
    """Search for a regex pattern in a file or directory.

    Returns matching lines with file:line prefix. Capped at 100 matches.
    """
    import re

    if root is not None:
        path = root
    pattern = pattern or query or text
    if not pattern:
        return "ERROR: pattern is required (aliases: query, text)"

    p = _resolve(path)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"INVALID_PATTERN: {e}"

    matches = []
    pattern_glob = glob or "*.py"
    files = (
        list(p.rglob(pattern_glob))
        if (p.is_dir() and recursive)
        else list(p.glob(pattern_glob)) if p.is_dir() else [p]
    )
    for f in sorted(files):
        try:
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{f}:{i}: {line.rstrip()}")
                    if len(matches) >= 100:
                        matches.append("...(truncated at 100 matches)")
                        return "\n".join(matches)
        except Exception:
            continue
    return "\n".join(matches) or "(no matches)"


@mcp.tool()
def patch(path: str, old: str, new: str) -> str:
    """Replace the first exact occurrence of `old` with `new` in a file.

    Works on any text file. If the file is a .py file the result is validated
    (ast.parse + black + ruff) before writing — rejects if the patch breaks syntax.

    This is the preferred tool for targeted edits. You only need to quote the
    lines you are changing, not the whole file.

    Returns "OK: patched <path>" or "NOT_FOUND: literal not found in file" or
    "REJECTED: <reason>" if the .py guard fails.
    """
    p = _resolve(path)
    if not p.exists():
        return f"NOT_FOUND: {path} does not exist"
    source = p.read_text()
    if old not in source:
        return f"NOT_FOUND: literal not found in {path}"
    patched = source.replace(old, new, 1)
    if p.suffix == ".py":
        ok, formatted, err, note = _guard(patched, path)
        if not ok:
            return f"REJECTED: {err}"
        p.write_text(formatted)
        suffix = f" (auto-corrected: {note})" if note else ""
        return f"OK: patched {p}{suffix}"
    else:
        p.write_text(patched)
    return f"OK: patched {p}"


@mcp.tool()
def edit_file(path: str, instruction: str | dict | None = None, operations: list[dict] | None = None) -> str:
    """Apply structured deterministic edits to a text file.

    `instruction` must be a JSON object string with an `operations` array.
    Supported ops:
      replace       {"op":"replace","old":"...","new":"...","count":1}
      delete        {"op":"delete","old":"...","count":1}
      insert_after  {"op":"insert_after","anchor":"...","text":"..."}
      insert_before {"op":"insert_before","anchor":"...","text":"..."}
      append        {"op":"append","text":"..."}
      prepend       {"op":"prepend","text":"..."}
    """
    p = _resolve(path)
    if not p.exists():
        return f"NOT_FOUND: {path} does not exist"

    try:
        spec = _parse_json_spec(instruction, "edit_file")
        operations = operations or spec.get("operations")
        if not isinstance(operations, list) or not operations:
            return "ERROR: edit_file requires a non-empty operations array"
        source = p.read_text(errors="replace")
        edited = _apply_text_operations(source, operations)
        ok, message = _write_text(p, edited)
        if not ok:
            return message
        diff = "".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                p.read_text(errors="replace").splitlines(keepends=True),
                fromfile=f"{path} (before)",
                tofile=f"{path} (after)",
            )
        )
        return message if not diff else f"{message}\n{diff}"
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool()
def edit_python(path: str, spec: str | dict | None = None, operations: list[dict] | None = None) -> str:
    """Apply deterministic Python-aware structural edits.

    `spec` must be a JSON object string with an `operations` array.
    Supported ops:
      add_import            {"op":"add_import","module":"pathlib","name":"Path"}
            remove_import         {"op":"remove_import","module":"pathlib","name":"Path"}
      add_function          {"op":"add_function","name":"foo","signature":"x: int","body":"return x + 1"}
      replace_function      {"op":"replace_function","name":"foo","code":"def foo(...): ..."}
      replace_function_body {"op":"replace_function_body","name":"foo","body":"return 123"}
      delete_function       {"op":"delete_function","name":"foo"}
      add_class             {"op":"add_class","name":"Thing","body":"pass"}
            add_method            {"op":"add_method","class":"Thing","name":"run","signature":"self","body":"return 1"}
            replace_method_body   {"op":"replace_method_body","class":"Thing","name":"run","body":"return 1"}
            delete_method         {"op":"delete_method","class":"Thing","name":"run"}
      replace_class_body    {"op":"replace_class_body","name":"Thing","body":"def run(self):\n    return 1"}
            set_class_attribute   {"op":"set_class_attribute","class":"Thing","name":"DEBUG","value":"False"}
            add_decorator         {"op":"add_decorator","target":"method","class":"Thing","name":"run","decorator":"staticmethod"}
            replace_decorators    {"op":"replace_decorators","target":"function","name":"main","decorators":["click.command()"]}
      set_constant          {"op":"set_constant","name":"DEBUG","value":"False"}
      append_code           {"op":"append_code","code":"def bar():\n    return 1"}
    """
    p = _resolve_py(path)
    if not p.exists():
        return f"NOT_FOUND: {path} does not exist"

    try:
        data = _parse_json_spec(spec, "edit_python")
        operations = operations or data.get("operations")
        if not isinstance(operations, list) or not operations:
            return "ERROR: edit_python requires a non-empty operations array"
        source = p.read_text()
        edited = _edit_python_source(source, operations)
        ok, message = _write_text(p, edited)
        if not ok:
            return message
        diff = "".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                p.read_text().splitlines(keepends=True),
                fromfile=f"{path} (before)",
                tofile=f"{path} (after)",
            )
        )
        return message if not diff else f"{message}\n{diff}"
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool()
def create_file(path: str, content: str) -> str:
    """Create any file (not just .py) with the given content.

    Parent directories are created automatically.
    If the file already exists it is overwritten.
    .py files go through the ast.parse + black + ruff guard.

    Use this for non-Python files: Makefiles, configs, shell scripts, JSON, etc.
    """
    p = _resolve(path)
    if p.suffix == ".py":
        ok, formatted, err, note = _guard(content, path)
        if not ok:
            return f"REJECTED: {err}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(formatted)
        suffix = f" (auto-corrected: {note})" if note else ""
        return f"OK: wrote {len(formatted)} bytes to {p}{suffix}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"OK: wrote {len(content)} bytes to {p}"


@mcp.tool()
def scaffold_repo(spec: str | dict | None = None, root: str = ".") -> str:
    """Create a repository skeleton from a deterministic JSON spec.

    Supported templates:
      {"template":"python-package","name":"pkg-name","description":"..."}
      {"template":"python-cli","name":"cli-name","description":"..."}
      {"template":"mcp-server","name":"server-name","description":"..."}

    Optional fields:
      "dirs": ["src", "tests"]
      "files": {"README.md": "...", "src/app.py": "..."}
      "overwrite": true|false
    """
    try:
        data = _parse_json_spec(spec, "scaffold_repo")
    except Exception as exc:
        return f"ERROR: {exc}"

    repo = _resolve(root)
    template = data.get("template")
    name = data.get("name", repo.name)
    description = data.get("description", "")
    overwrite = bool(data.get("overwrite", False))

    dirs: list[str] = list(data.get("dirs", []))
    files: dict[str, str] = dict(data.get("files", {}))

    if template:
        if template == "python-package":
            tmpl_dirs, tmpl_files = _template_python_package(name, description)
        elif template == "python-cli":
            tmpl_dirs, tmpl_files = _template_python_cli(name, description)
        elif template == "mcp-server":
            tmpl_dirs, tmpl_files = _template_mcp_server(name, description)
        else:
            return f"ERROR: unsupported template '{template}'"
        dirs = tmpl_dirs + dirs
        files = {**tmpl_files, **files}

    created: list[str] = []
    for rel_dir in dirs:
        target = repo / rel_dir
        target.mkdir(parents=True, exist_ok=True)
        created.append(f"dir {target}")

    for rel_path, content in files.items():
        target = repo / rel_path
        if target.exists() and not overwrite:
            created.append(f"skip {target}")
            continue
        ok, message = _write_text(target, content)
        if not ok:
            return message
        created.append(message)

    return "OK:\n" + "\n".join(created)


@mcp.tool()
def find(pattern: str, path: str = ".") -> str:
    """Glob for files matching pattern under path.

    Examples:
      find("*.py", "/home/ziggibot/localexp")
      find("**/*.toml", ".")
      find("test_*.py", "src")

    Returns newline-separated absolute paths, capped at 200 results.
    """
    p = _resolve(path)
    if not p.exists():
        return f"NOT_FOUND: {path} does not exist"
    matches = sorted(p.glob(pattern))[:200]
    if not matches:
        return "(no matches)"
    return "\n".join(str(m) for m in matches)


@mcp.tool()
def locate_paths(
    query: str | None = None,
    root: str = ".",
    limit: int = 100,
    kind: str = "any",
    pattern: str | None = None,
    filename: str | None = None,
    path: str | None = None,
) -> str:
    """Find files or directories quickly by path/name.

    Uses the fastest backend available:
    1. plocate/locate for indexed machine-wide search when installed
    2. ripgrep file listing + path filtering otherwise

    Args:
    query: file/path fragment, basename, or glob-like text
    pattern/filename: ergonomic aliases for query
    root/path: search root aliases
      limit: max results (default 100)
      kind: one of 'any', 'file', 'dir'

    Returns newline-separated absolute paths, ranked with exact basename/path
    matches first.
    """
    import shutil

    if kind not in {"any", "file", "dir"}:
        return "ERROR: kind must be one of any|file|dir"

    if path is not None:
        root = path
    if query is None:
        query = pattern or filename
    query = (query or "").strip()
    if not query:
        return "ERROR: query is required (aliases: pattern, filename)"

    limit = max(1, min(int(limit), 500))
    root_path = _resolve(root)

    index_name = _best_index_name(root_path)
    if index_name:
        candidates = _search_index(query, index_name, root_path, limit, kind)
        if candidates:
            return "\n".join(candidates)

    locate_bin = shutil.which("plocate") or shutil.which("locate")
    if locate_bin:
        try:
            r = subprocess.run(
                [locate_bin, "-i", "--", query],
                capture_output=True,
                text=True,
                timeout=10,
            )
            candidates = [line.strip() for line in r.stdout.splitlines() if line.strip()]
            if root_path != Path.cwd():
                root_text = str(root_path)
                candidates = [item for item in candidates if item.startswith(root_text)]
            if kind != "any":
                filtered = []
                for item in candidates:
                    path_obj = Path(item)
                    if kind == "file" and path_obj.is_file():
                        filtered.append(item)
                    elif kind == "dir" and path_obj.is_dir():
                        filtered.append(item)
                candidates = filtered
            if candidates:
                return "\n".join(sorted(candidates, key=lambda item: _rank_path_match(item, query))[:limit])
        except Exception:
            pass

    if not root_path.exists():
        return f"NOT_FOUND: {root} does not exist"

    glob_like = query if any(ch in query for ch in "*?[]") else f"*{query}*"
    rg_cmd = ["rg", "--files", str(root_path), "-g", glob_like]
    try:
        r = subprocess.run(rg_cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "[timeout after 20s]"

    candidates = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    if kind != "any":
        filtered = []
        for item in candidates:
            path_obj = Path(item)
            if kind == "file" and path_obj.is_file():
                filtered.append(item)
            elif kind == "dir" and path_obj.is_dir():
                filtered.append(item)
        candidates = filtered

    if not candidates:
        return "(no matches)"
    return "\n".join(sorted(candidates, key=lambda item: _rank_path_match(item, query))[:limit])


@mcp.tool()
def build_path_index(root: str = ".", name: str | None = None) -> str:
    """Build or rebuild a persistent SQLite path index for fast repeated searches.

    Indexes both files and directories under `root`.
    If `name` is omitted, uses the resolved root path as the index name.
    """
    root_path = _resolve(root)
    if not root_path.exists() or not root_path.is_dir():
        return f"NOT_FOUND: {root} does not exist or is not a directory"

    index_name = name or str(root_path)
    rows: list[tuple[str, str, str, str, str]] = []

    for dirpath, dirnames, filenames in os.walk(root_path):
        current = Path(dirpath)
        rows.append((index_name, str(current), current.name.lower(), str(current).lower(), "dir"))
        for dirname in dirnames:
            path_obj = current / dirname
            rows.append((index_name, str(path_obj), dirname.lower(), str(path_obj).lower(), "dir"))
        for filename in filenames:
            path_obj = current / filename
            rows.append((index_name, str(path_obj), filename.lower(), str(path_obj).lower(), "file"))

    with _path_index_con() as con:
        con.execute("DELETE FROM path_entries WHERE index_name = ?", (index_name,))
        con.execute("DELETE FROM path_indexes WHERE name = ?", (index_name,))
        con.executemany(
            "INSERT INTO path_entries (index_name, path, basename, lower_path, kind) VALUES (?,?,?,?,?)",
            rows,
        )
        con.execute(
            "INSERT INTO path_indexes (name, root, built_at, entry_count) VALUES (?,?,?,?)",
            (index_name, str(root_path), datetime.now(timezone.utc).isoformat(), len(rows)),
        )
        con.commit()

    return f"OK: built path index '{index_name}' for {root_path} with {len(rows)} entries"


@mcp.tool()
def list_path_indexes() -> str:
    """List available persistent path indexes."""
    with _path_index_con() as con:
        rows = con.execute(
            "SELECT name, root, built_at, entry_count FROM path_indexes ORDER BY built_at DESC"
        ).fetchall()
    if not rows:
        return "(no path indexes)"
    return "\n".join(
        f"{name} | root={root} | entries={entry_count} | built_at={built_at}"
        for name, root, built_at, entry_count in rows
    )


@mcp.tool()
def search_path_index(
    query: str | None = None,
    root: str = ".",
    index_name: str | None = None,
    limit: int = 100,
    kind: str = "any",
    pattern: str | None = None,
    filename: str | None = None,
    path: str | None = None,
) -> str:
    """Search a previously built path index.

    If index_name is omitted, uses the best matching index for `root`.
    This is the fastest path search once an index exists.

    Accepts ergonomic aliases:
      pattern/filename -> query
      path -> root
    """
    if kind not in {"any", "file", "dir"}:
        return "ERROR: kind must be one of any|file|dir"
    if path is not None:
        root = path
    if query is None:
        query = pattern or filename
    query = (query or "").strip()
    if not query:
        return "ERROR: query is required (aliases: pattern, filename)"
    root_path = _resolve(root)
    index = index_name or _best_index_name(root_path)
    if not index:
        return "NOT_FOUND: no path index available for this root"
    matches = _search_index(query, index, root_path, max(1, min(int(limit), 500)), kind)
    return "\n".join(matches) if matches else "(no matches)"


@mcp.tool()
def verify_change(
    path: str,
    expectation: str | dict | None = None,
    exists: bool | None = None,
    contains: list[str] | None = None,
    not_contains: list[str] | None = None,
    python_parse: bool | None = None,
    ruff_clean: bool | None = None,
    run_spec: dict | None = None,
) -> str:
    """Verify a change with deterministic checks.

    `expectation` must be a JSON object string. Supported keys:
      exists: true
      contains: ["literal", ...]
      not_contains: ["literal", ...]
      python_parse: true
      ruff_clean: true
      run: {"code":"...", "cwd":"...", "stdout_contains":"...", "exit_code":0}
    """
    try:
        spec = _parse_json_spec(expectation, "verify_change")
    except Exception as exc:
        return f"ERROR: {exc}"

    if exists is not None:
        spec["exists"] = exists
    if contains is not None:
        spec["contains"] = contains
    if not_contains is not None:
        spec["not_contains"] = not_contains
    if python_parse is not None:
        spec["python_parse"] = python_parse
    if ruff_clean is not None:
        spec["ruff_clean"] = ruff_clean
    if run_spec is not None:
        spec["run"] = run_spec

    p = _resolve(path)
    checks: list[dict] = []

    if "exists" in spec:
        ok = p.exists() is bool(spec["exists"])
        checks.append({"check": "exists", "ok": ok})

    content = p.read_text(errors="replace") if p.exists() and p.is_file() else ""

    for literal in spec.get("contains", []):
        checks.append({"check": f"contains:{literal[:40]}", "ok": literal in content})
    for literal in spec.get("not_contains", []):
        checks.append({"check": f"not_contains:{literal[:40]}", "ok": literal not in content})

    if spec.get("python_parse"):
        try:
            ast.parse(content)
            checks.append({"check": "python_parse", "ok": True})
        except Exception as exc:
            checks.append({"check": "python_parse", "ok": False, "detail": str(exc)})

    if spec.get("ruff_clean"):
        result = check(str(p))
        checks.append({"check": "ruff_clean", "ok": result == "OK: no issues", "detail": result[:200]})

    if "run" in spec:
        run_spec = spec["run"]
        outcome = run(run_spec.get("code", ""), run_spec.get("cwd"))
        ok = True
        if "stdout_contains" in run_spec:
            ok = ok and run_spec["stdout_contains"] in outcome
        if "exit_code" in run_spec:
            ok = ok and f"[exit {run_spec['exit_code']}]" in outcome
        checks.append({"check": "run", "ok": ok, "detail": outcome[:300]})

    passed = all(item.get("ok") for item in checks) if checks else False
    return json.dumps({"ok": passed, "checks": checks}, indent=2)


@mcp.tool()
def verify_repo(
    root: str = ".",
    spec: str | dict | None = None,
    must_exist: list[str] | None = None,
    py_glob: str | None = None,
    parse_python: bool | None = None,
    ruff_clean: bool | None = None,
    contains: list[dict] | None = None,
    commands: list[dict] | None = None,
) -> str:
    """Verify repository structure and health with deterministic checks.

    If `spec` is omitted, defaults to:
      - root exists
      - all Python files parse
      - repo is ruff clean

    `spec` may be a JSON object string with keys:
      must_exist: ["README.md", "src"]
      py_glob: "**/*.py"
      parse_python: true
      ruff_clean: true
      contains: [{"path":"README.md","text":"project"}]
      commands: [{"cmd":"pytest -q","exit_code":0}]
    """
    try:
        data = _parse_json_spec(spec, "verify_repo") if spec is not None else {}
    except Exception as exc:
        return f"ERROR: {exc}"

    if must_exist is not None:
        data["must_exist"] = must_exist
    if py_glob is not None:
        data["py_glob"] = py_glob
    if parse_python is not None:
        data["parse_python"] = parse_python
    if ruff_clean is not None:
        data["ruff_clean"] = ruff_clean
    if contains is not None:
        data["contains"] = contains
    if commands is not None:
        data["commands"] = commands

    repo = _resolve(root)
    py_glob = data.get("py_glob", "**/*.py")
    parse_python = data.get("parse_python", True)
    ruff_clean = data.get("ruff_clean", True)
    must_exist = data.get("must_exist", [])
    contains = data.get("contains", [])
    commands = data.get("commands", [])

    checks: list[dict] = [{"check": "root_exists", "ok": repo.exists()}]
    if not repo.exists():
        return json.dumps({"ok": False, "checks": checks}, indent=2)

    for rel in must_exist:
        target = repo / rel
        checks.append({"check": f"exists:{rel}", "ok": target.exists()})

    py_files = sorted(repo.glob(py_glob))
    if parse_python:
        for file_path in py_files:
            try:
                ast.parse(file_path.read_text())
                checks.append({"check": f"parse:{file_path.relative_to(repo)}", "ok": True})
            except Exception as exc:
                checks.append({"check": f"parse:{file_path.relative_to(repo)}", "ok": False, "detail": str(exc)})

    if ruff_clean:
        result = shell(f'"{_RUFF}" check --output-format=json .', cwd=str(repo))
        ok = result == "[exit 0]" or result.endswith("\n[exit 0]")
        checks.append({"check": "ruff_clean", "ok": ok, "detail": result[:400]})

    for item in contains:
        rel = item["path"]
        text = item["text"]
        target = repo / rel
        ok = target.exists() and text in target.read_text(errors="replace")
        checks.append({"check": f"contains:{rel}:{text[:30]}", "ok": ok})

    for item in commands:
        outcome = shell(item["cmd"], cwd=str(repo))
        ok = True
        if "exit_code" in item:
            ok = ok and f"[exit {item['exit_code']}]" in outcome
        if "stdout_contains" in item:
            ok = ok and item["stdout_contains"] in outcome
        checks.append({"check": f"cmd:{item['cmd'][:40]}", "ok": ok, "detail": outcome[:400]})

    return json.dumps({"ok": all(c.get("ok") for c in checks), "checks": checks}, indent=2)


@mcp.tool()
def save_verify_profile(name: str, spec: str | dict) -> str:
    """Save a reusable verify_repo spec under a profile name.

    This is explicit learning: the agent can create and reuse profiles based on
    successful verification patterns instead of relying on built-in heuristics.
    """
    try:
        data = _parse_json_spec(spec, "save_verify_profile")
    except Exception as exc:
        return f"ERROR: {exc}"

    _VERIFY_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    target = _VERIFY_PROFILE_DIR / f"{name}.json"
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return f"OK: saved verify profile {name} to {target}"


@mcp.tool()
def list_verify_profiles() -> str:
    """List saved verify profiles."""
    if not _VERIFY_PROFILE_DIR.exists():
        return "(no profiles)"
    items = sorted(p.stem for p in _VERIFY_PROFILE_DIR.glob("*.json"))
    return "\n".join(items) if items else "(no profiles)"


@mcp.tool()
def verify_with_profile(name: str, root: str = ".", overrides: str | dict | None = None) -> str:
    """Run verify_repo using a saved profile, optionally merged with overrides."""
    target = _VERIFY_PROFILE_DIR / f"{name}.json"
    if not target.exists():
        return f"NOT_FOUND: verify profile {name}"
    base = json.loads(target.read_text())
    if overrides:
        try:
            override_data = _parse_json_spec(overrides, "verify_with_profile")
        except Exception as exc:
            return f"ERROR: {exc}"
        base = {**base, **override_data}
    return verify_repo(root, json.dumps(base))


@mcp.tool()
def save_verify_profile_from_repo(name: str, root: str = ".", spec: str | dict | None = None) -> str:
    """Verify a repo, and if it passes, persist the verify spec as a reusable profile.

    This is explicit learning from success, not heuristic guessing.
    """
    verify_spec = spec or "{}"
    result = verify_repo(root, verify_spec)
    try:
        parsed = json.loads(result)
    except Exception:
        return f"ERROR: verify_repo returned non-JSON result: {result[:300]}"
    if not parsed.get("ok"):
        return f"ERROR: repository did not pass verification; profile not saved\n{result}"
    return save_verify_profile(name, verify_spec)


def _expand_workflow_template(data: dict) -> dict:
    """Expand a compact workflow template into a generic workflow spec."""
    template = data.get("template")
    if not template:
        return data

    stop_on_error = bool(data.get("stop_on_error", True))

    if template == "scaffold_and_verify":
        if "scaffold" not in data or "verify" not in data:
            raise ValueError("template scaffold_and_verify requires scaffold and verify objects")
        root = data.get("root", ".")
        workflow = {
            "steps": [
                {"tool": "scaffold_repo", "args": {"spec": json.dumps(data["scaffold"]), "root": root}},
                {"tool": "verify_repo", "args": {"root": root, "spec": json.dumps(data["verify"])}} ,
            ],
            "stop_on_error": stop_on_error,
        }
        if data.get("save_profile_on_success"):
            workflow["save_profile_on_success"] = data["save_profile_on_success"]
        return workflow

    if template == "edit_and_verify":
        steps = data.get("edits")
        if not isinstance(steps, list) or not steps:
            raise ValueError("template edit_and_verify requires a non-empty edits array")
        workflow_steps = []
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict) or "tool" not in step or "args" not in step:
                raise ValueError(f"edit step {index} must contain tool and args")
            workflow_steps.append(step)
        if "verify" in data:
            verify = data["verify"]
            if data.get("verify_profile"):
                workflow_steps.append(
                    {
                        "tool": "verify_with_profile",
                        "args": {
                            "name": data["verify_profile"],
                            "root": data.get("root", "."),
                            "overrides": json.dumps(verify) if verify else None,
                        },
                    }
                )
            else:
                workflow_steps.append(
                    {"tool": "verify_repo", "args": {"root": data.get("root", "."), "spec": json.dumps(verify)}}
                )
        workflow = {"steps": workflow_steps, "stop_on_error": stop_on_error}
        if data.get("save_profile_on_success"):
            workflow["save_profile_on_success"] = data["save_profile_on_success"]
        return workflow

    if template == "patch_reload_and_verify":
        patch_spec = data.get("patch")
        verify = data.get("verify")
        if not isinstance(patch_spec, dict) or not verify:
            raise ValueError("template patch_reload_and_verify requires patch and verify objects")
        workflow = {
            "steps": [
                {"tool": patch_spec.get("tool", "patch"), "args": patch_spec.get("args", {})},
                {"tool": "reload_agent_tools", "args": {}},
                {"tool": "verify_repo", "args": {"root": data.get("root", "."), "spec": json.dumps(verify)}},
            ],
            "stop_on_error": stop_on_error,
        }
        if data.get("save_profile_on_success"):
            workflow["save_profile_on_success"] = data["save_profile_on_success"]
        return workflow

    if template == "self_improve_agent":
        edits = data.get("edits")
        if not isinstance(edits, list) or not edits:
            raise ValueError("template self_improve_agent requires a non-empty edits array")

        workflow_steps = []
        for index, step in enumerate(edits, 1):
            if not isinstance(step, dict):
                raise ValueError(f"self_improve edit step {index} must be an object")
            tool_name = step.get("tool") or step.get("action")
            args = step.get("args")
            if not tool_name or not isinstance(args, dict):
                raise ValueError(f"self_improve edit step {index} must contain tool/action and args")
            workflow_steps.append({"tool": tool_name, "args": args})

        if data.get("reload_tools", True):
            workflow_steps.append({"tool": "reload_agent_tools", "args": {}})

        verify = data.get("verify")
        if verify:
            workflow_steps.append(
                {"tool": "verify_repo", "args": {"root": data.get("root", "."), "spec": json.dumps(verify)}}
            )

        smoke_tests = data.get("smoke_tests", [])
        if smoke_tests is None:
            smoke_tests = []
        if not isinstance(smoke_tests, list):
            raise ValueError("template self_improve_agent smoke_tests must be a list")
        for index, smoke in enumerate(smoke_tests, 1):
            if not isinstance(smoke, dict) or "code" not in smoke:
                raise ValueError(f"smoke test {index} must be an object with code")
            workflow_steps.append(
                {
                    "tool": "run",
                    "args": {
                        "code": smoke["code"],
                        "cwd": smoke.get("cwd", data.get("root", ".")),
                    },
                }
            )

        workflow = {"steps": workflow_steps, "stop_on_error": stop_on_error}
        if data.get("save_profile_on_success"):
            workflow["save_profile_on_success"] = data["save_profile_on_success"]
        return workflow

    raise ValueError(f"unsupported workflow template '{template}'")


@mcp.tool()
def execute_workflow(spec: str | dict) -> str:
    """Execute a deterministic workflow spec using existing tools.

        `spec` must be a JSON object string with either:
      steps: [
        {"tool":"edit_python","args":{...}},
        {"tool":"create_file","args":{...}},
        {"tool":"verify_repo","args":{...}},
      ]

        Or a compact template:
            {"template":"scaffold_and_verify", ...}
            {"template":"edit_and_verify", ...}
            {"template":"patch_reload_and_verify", ...}

    Optional fields:
      stop_on_error: true|false   (default true)
      save_profile_on_success: {"name":"profile-name","root":".","spec":"{...}"}

        Supported step tools:
            read, write, patch, edit_file, edit_python, fix, create_file, find,
            locate_paths, build_path_index, list_path_indexes, search_path_index,
            scaffold_repo, transform, rename, rename_symbol, check, py_tree, ls,
            shell, grep, verify_change, verify_repo, save_verify_profile,
            save_verify_profile_from_repo, list_verify_profiles, verify_with_profile,
            reload_agent_tools, run, searxng
    """
    try:
        data = _parse_json_spec(spec, "execute_workflow")
    except Exception as exc:
        return f"ERROR: {exc}"

    try:
        data = _expand_workflow_template(data)
    except Exception as exc:
        return f"ERROR: {exc}"

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        return "ERROR: execute_workflow requires a non-empty steps array"

    stop_on_error = bool(data.get("stop_on_error", True))

    tool_map = {
        "read": read,
        "write": write,
        "patch": patch,
        "edit_file": edit_file,
        "edit_python": edit_python,
        "fix": fix,
        "create_file": create_file,
        "find": find,
        "locate_paths": locate_paths,
        "build_path_index": build_path_index,
        "list_path_indexes": list_path_indexes,
        "search_path_index": search_path_index,
        "scaffold_repo": scaffold_repo,
        "transform": transform,
        "rename": rename,
        "rename_symbol": rename_symbol,
        "check": check,
        "py_tree": tree,
        "ls": ls,
        "shell": shell,
        "grep": grep,
        "verify_change": verify_change,
        "verify_repo": verify_repo,
        "save_verify_profile": save_verify_profile,
        "save_verify_profile_from_repo": save_verify_profile_from_repo,
        "list_verify_profiles": list_verify_profiles,
        "verify_with_profile": verify_with_profile,
        "reload_agent_tools": reload_agent_tools,
        "run": run,
        "searxng": searxng,
    }

    results: list[dict] = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            return f"ERROR: step {index} must be an object"
        tool_name = step.get("tool")
        args = step.get("args", {})
        if tool_name not in tool_map:
            return f"ERROR: step {index} uses unsupported tool '{tool_name}'"
        if not isinstance(args, dict):
            return f"ERROR: step {index} args must be an object"

        try:
            output = tool_map[tool_name](**args)
        except TypeError as exc:
            return f"ERROR: step {index} bad args for {tool_name}: {exc}"

        output_text = output if isinstance(output, str) else json.dumps(output)
        ok = not (
            output_text.startswith("ERROR:")
            or output_text.startswith("REJECTED:")
            or output_text.startswith("NOT_FOUND:")
        )
        results.append({"step": index, "tool": tool_name, "ok": ok, "output": output_text[:2000]})
        if stop_on_error and not ok:
            return json.dumps({"ok": False, "results": results}, indent=2)

    profile = data.get("save_profile_on_success")
    if profile:
        if not isinstance(profile, dict) or "name" not in profile:
            return "ERROR: save_profile_on_success must be an object with at least a name"
        save_result = save_verify_profile_from_repo(
            profile["name"],
            profile.get("root", "."),
            profile.get("spec"),
        )
        results.append({"step": len(results) + 1, "tool": "save_verify_profile_from_repo", "ok": not save_result.startswith("ERROR:"), "output": save_result[:2000]})
        if save_result.startswith("ERROR:") and stop_on_error:
            return json.dumps({"ok": False, "results": results}, indent=2)

    return json.dumps({"ok": all(item["ok"] for item in results), "results": results}, indent=2)


@mcp.tool()
def fix(path: str) -> str:
    """Auto-fix all fixable lint and formatting issues in a .py file.

    Runs black then ruff --fix and returns a unified diff of what changed.
    If nothing changed returns 'OK: already clean'.
    Use this instead of check() + patch() loops — it does the work for you.
    """
    import difflib

    p = _resolve_py(path)
    original = p.read_text()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(original)
        tmp = Path(f.name)

    try:
        subprocess.run([_BLACK, "--quiet", str(tmp)], capture_output=True)
        subprocess.run(
            [_RUFF, "check", "--fix", "--quiet", str(tmp)], capture_output=True
        )
        fixed = tmp.read_text()
    finally:
        tmp.unlink(missing_ok=True)

    if fixed == original:
        return "OK: already clean"

    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile=f"{path} (before)",
            tofile=f"{path} (after)",
        )
    )
    p.write_text(fixed)
    return f"OK: fixed\n{diff}"


@mcp.tool()
def reload_agent_tools() -> str:
    """Signal agent.py to reload server.py tools from disk.

    Writes a sentinel file that agent.py watches. Run this after patching server.py
    to pick up new tools without restarting the agent.
    """
    sentinel = Path(__file__).parent.parent / ".reload_tools"
    sentinel.write_text("reload")
    return "OK: reload sentinel written — tools will reload on next turn"


@mcp.tool()
def rename_symbol(
    path: str,
    new_name: str,
    symbol: str | None = None,
    snippet: str | None = None,
    line: int | None = None,
    col: int | None = None,
) -> str:
    """Ergonomic rename helper.

    Lets the caller identify the symbol by line/col, exact snippet, or symbol name.
    Uses the first match when only symbol is provided.
    """
    import re

    p = _resolve_py(path)
    source = p.read_text()
    lines = source.splitlines(keepends=True)

    def _fallback_single_file_rename(old_name: str) -> str:
        rx = re.compile(rf"\b{re.escape(old_name)}\b")
        if not rx.search(source):
            return f"NOT_FOUND: symbol '{old_name}' not found"
        replaced = rx.sub(new_name, source)
        ok, formatted, err, note = _guard(replaced, path)
        if not ok:
            return f"REJECTED: {err}"
        p.write_text(formatted)
        suffix = f" (auto-corrected: {note})" if note else ""
        return f"OK: renamed symbol to '{new_name}'{suffix}"

    if line is not None and col is not None:
        result = rename(path, line, col, new_name)
        if not result.startswith("ROPE_ERROR:"):
            return result

    if snippet:
        idx = source.find(snippet)
        if idx == -1:
            return "NOT_FOUND: snippet not found"
        line_no = source.count("\n", 0, idx) + 1
        line_start = source.rfind("\n", 0, idx) + 1
        result = rename(path, line_no, idx - line_start, new_name)
        if not result.startswith("ROPE_ERROR:"):
            return result
        token = snippet.strip().split()[0]
        return _fallback_single_file_rename(token)

    if symbol:
        for line_no, line_text in enumerate(lines, 1):
            col_no = line_text.find(symbol)
            if col_no != -1:
                result = rename(path, line_no, col_no, new_name)
                if not result.startswith("ROPE_ERROR:"):
                    return result
                return _fallback_single_file_rename(symbol)
        return f"NOT_FOUND: symbol '{symbol}' not found"

    return "ERROR: provide line+col, snippet, or symbol"


@mcp.tool()
def run(code: str, cwd: str | None = None) -> str:
    """Execute Python code and return stdout, stderr, and exit code.

    The proof tool — instead of asserting what code does, run it and see.
    stdout+stderr capped at 4000 chars. Timeout: 30s.
    cwd defaults to the agent's working directory (parent of MCP/).
    """
    import tempfile as _tf

    with _tf.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp = Path(f.name)

    env_path = str(_VENV_BIN) + ":" + __import__("os").environ.get("PATH", "")
    try:
        r = subprocess.run(
            [str(_VENV_BIN / "python"), str(tmp)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd or str(Path(__file__).parent.parent),
            env={**__import__("os").environ, "PATH": env_path},
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr] {err}")
        parts.append(f"[exit {r.returncode}]")
        result = "\n".join(parts)
        return result[:4000] + ("...(truncated)" if len(result) > 4000 else "")
    except subprocess.TimeoutExpired:
        return "[timeout after 30s]"
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    import sys as _sys

    transport = "stdio"
    port = 8765
    args = _sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--transport" and i + 1 < len(args):
            transport = args[i + 1]
        elif arg == "--port" and i + 1 < len(args):
            port = int(args[i + 1])

    if transport == "sse":
        _mcp = FastMCP("python-transformer", port=port)
        for _attr in list(globals().values()):
            if callable(_attr) and hasattr(_attr, "_mcp_tool"):
                _mcp.tool()(_attr)
        _mcp.run(transport="sse")
    else:
        mcp.run()
