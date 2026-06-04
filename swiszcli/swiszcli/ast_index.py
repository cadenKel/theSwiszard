"""AST symbol indexer: scan a project root for Python symbols and their locations."""
from __future__ import annotations
import ast
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

INDEX_DB_DEFAULT = Path.home() / '.swiszcli' / 'ast_index.db'

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    qualname    TEXT NOT NULL,
    lineno      INTEGER NOT NULL,
    end_lineno  INTEGER NOT NULL,
    docstring   TEXT,
    indexed_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sym_proj ON symbols(project_id);
CREATE INDEX IF NOT EXISTS idx_sym_path ON symbols(path);
CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_sym_qual ON symbols(qualname);
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    mtime       REAL NOT NULL,
    indexed_at  REAL NOT NULL
);
"""

@dataclass
class Symbol:
    project_id: str
    path: str
    kind: str
    name: str
    qualname: str
    lineno: int
    end_lineno: int
    docstring: str = ''

class ASTIndex:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path or INDEX_DB_DEFAULT)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def index_file(self, path, project_id=''):
        p = Path(path).resolve()
        if p.suffix != '.py' or not p.exists():
            return {'action': 'skip', 'reason': 'not python or missing'}
        mtime = p.stat().st_mtime
        row = self._conn.execute('SELECT mtime FROM files WHERE path = ?', (str(p),)).fetchone()
        if row and row['mtime'] == mtime:
            return {'action': 'unchanged', 'path': str(p)}
        try:
            src = p.read_text()
            tree = ast.parse(src)
        except Exception as e:
            return {'action': 'parse_error', 'path': str(p), 'error': str(e)[:200]}
        # delete old symbols for this file
        self._conn.execute('DELETE FROM symbols WHERE path = ?', (str(p),))
        syms = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = 'function'
            elif isinstance(node, ast.ClassDef):
                kind = 'class'
            else:
                continue
            qual = _qualname(tree, node)
            doc = ast.get_docstring(node) or ''
            syms.append(Symbol(
                project_id=project_id, path=str(p), kind=kind, name=node.name,
                qualname=qual, lineno=node.lineno, end_lineno=node.end_lineno or node.lineno,
                docstring=doc[:500],
            ))
        for s in syms:
            self._conn.execute(
                'INSERT INTO symbols(project_id, path, kind, name, qualname, lineno, end_lineno, docstring, indexed_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (s.project_id, s.path, s.kind, s.name, s.qualname, s.lineno, s.end_lineno, s.docstring, time.time()),
            )
        self._conn.execute(
            'INSERT OR REPLACE INTO files(path, mtime, indexed_at) VALUES (?, ?, ?)',
            (str(p), mtime, time.time()),
        )
        self._conn.commit()
        return {'action': 'indexed', 'path': str(p), 'symbols': len(syms)}

    def index_project(self, root, project_id='', exclude=None):
        exclude = exclude or {'.git', '__pycache__', '.venv', 'venv', 'node_modules', '.tox', 'dist', 'build'}
        r = Path(root)
        if not r.exists():
            return {'indexed': 0, 'skipped': 0, 'errors': 0}
        counts = {'indexed': 0, 'unchanged': 0, 'errors': 0}
        for p in r.rglob('*.py'):
            if any(part in exclude for part in p.parts):
                continue
            try:
                res = self.index_file(p, project_id=project_id)
                if res['action'] == 'indexed':
                    counts['indexed'] += 1
                elif res['action'] == 'unchanged':
                    counts['unchanged'] += 1
                elif res['action'] == 'parse_error':
                    counts['errors'] += 1
            except Exception:
                counts['errors'] += 1
        return counts

    def find_symbol(self, name, project_id=None):
        if project_id:
            rows = self._conn.execute(
                'SELECT * FROM symbols WHERE name = ? AND project_id = ?',
                (name, project_id),
            ).fetchall()
        else:
            rows = self._conn.execute('SELECT * FROM symbols WHERE name = ?', (name,)).fetchall()
        return [dict(r) for r in rows]

    def find_in_file(self, path):
        rows = self._conn.execute(
            'SELECT * FROM symbols WHERE path = ? ORDER BY lineno',
            (str(path),),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, project_id=None):
        if project_id:
            sym = self._conn.execute('SELECT COUNT(*) FROM symbols WHERE project_id = ?', (project_id,)).fetchone()[0]
            files = self._conn.execute('SELECT COUNT(DISTINCT path) FROM symbols WHERE project_id = ?', (project_id,)).fetchone()[0]
        else:
            sym = self._conn.execute('SELECT COUNT(*) FROM symbols').fetchone()[0]
            files = self._conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        return {'symbols': sym, 'files': files}

def _qualname(tree, target):
    # Walk to find target's parent chain by re-traversing with stack
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    parts = [target.name]
    cur = parents.get(id(target))
    while cur is not None and not isinstance(cur, ast.Module):
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(cur.name)
        cur = parents.get(id(cur))
    return '.'.join(reversed(parts))

