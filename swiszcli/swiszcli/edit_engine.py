"""Edit engine: AST-aware diff-based file editing with full undo history."""
from __future__ import annotations
import ast
import difflib
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

EDIT_DB_DEFAULT = Path.home() / '.swiszcli' / 'edits.db'
SNAPSHOT_DIR_DEFAULT = Path.home() / '.swiszcli' / 'snapshots'

SCHEMA = """
CREATE TABLE IF NOT EXISTS edits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    session_id    TEXT,
    project_id    TEXT,
    path          TEXT    NOT NULL,
    pre_sha       TEXT    NOT NULL,
    post_sha      TEXT    NOT NULL,
    snapshot_pre  TEXT    NOT NULL,
    snapshot_post TEXT    NOT NULL,
    diff          TEXT    NOT NULL,
    description   TEXT    DEFAULT '',
    reverted      INTEGER NOT NULL DEFAULT 0,
    reverted_at   REAL    DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_edits_path ON edits(path);
CREATE INDEX IF NOT EXISTS idx_edits_session ON edits(session_id);
CREATE INDEX IF NOT EXISTS idx_edits_ts ON edits(ts);
"""

def _sha(text):
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()[:16]

@dataclass
class EditProposal:
    path: str
    pre_text: str
    post_text: str
    diff: str
    pre_sha: str
    post_sha: str
    description: str = ''
    def is_noop(self):
        return self.pre_sha == self.post_sha
    def render_preview(self, max_lines=80):
        lines = self.diff.splitlines()
        if len(lines) <= max_lines:
            return self.diff
        return chr(10).join(lines[:max_lines]) + chr(10) + '... [' + str(len(lines) - max_lines) + ' more lines]'

class EditEngine:
    def __init__(self, db_path=None, snapshot_dir=None):
        self.db_path = Path(db_path or EDIT_DB_DEFAULT)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir = Path(snapshot_dir or SNAPSHOT_DIR_DEFAULT)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def propose_replace(self, path, old_text, new_text, description=''):
        """Replace a unique substring within a file. Fails loudly if not unique."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError('no such file: ' + str(path))
        pre = p.read_text()
        cnt = pre.count(old_text)
        if cnt == 0:
            raise ValueError('old_text not found in ' + str(path))
        if cnt > 1:
            raise ValueError('old_text matches ' + str(cnt) + ' locations in ' + str(path) + ' (need unique)')
        post = pre.replace(old_text, new_text, 1)
        return self._make_proposal(str(p), pre, post, description)

    def propose_full(self, path, new_text, description=''):
        """Replace entire file content."""
        p = Path(path)
        pre = p.read_text() if p.exists() else ''
        return self._make_proposal(str(p), pre, new_text, description)

    def propose_ast_replace(self, path, symbol_name, new_source, description=''):
        """AST-aware replacement: replace a function/class body by name. Python only."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError('no such file: ' + str(path))
        if p.suffix != '.py':
            raise ValueError('ast_replace only supports .py files (got ' + p.suffix + ')')
        pre = p.read_text()
        tree = ast.parse(pre)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol_name:
                    target = node
                    break
        if target is None:
            raise ValueError('symbol not found: ' + symbol_name)
        lines = pre.splitlines(keepends=True)
        start = target.lineno - 1
        end = (target.end_lineno or target.lineno)
        # collect decorator lines above
        dec_start = start
        if getattr(target, 'decorator_list', None):
            for dec in target.decorator_list:
                if dec.lineno - 1 < dec_start:
                    dec_start = dec.lineno - 1
        old_block = ''.join(lines[dec_start:end])
        if not new_source.endswith(chr(10)):
            new_source = new_source + chr(10)
        post = ''.join(lines[:dec_start]) + new_source + ''.join(lines[end:])
        return self._make_proposal(str(p), pre, post, description or ('ast_replace ' + symbol_name))

    def _make_proposal(self, path, pre, post, description):
        pre_sha = _sha(pre)
        post_sha = _sha(post)
        diff = ''.join(difflib.unified_diff(
            pre.splitlines(keepends=True),
            post.splitlines(keepends=True),
            fromfile='a/' + path, tofile='b/' + path,
        ))
        return EditProposal(
            path=path, pre_text=pre, post_text=post, diff=diff,
            pre_sha=pre_sha, post_sha=post_sha, description=description,
        )

    def apply(self, proposal, session_id='', project_id=''):
        """Apply an edit and log it. Returns the edit id."""
        if proposal.is_noop():
            return {'action': 'noop', 'id': None}
        p = Path(proposal.path)
        # Verify file hasn't changed under us since proposal was made
        current = p.read_text() if p.exists() else ''
        if _sha(current) != proposal.pre_sha:
            raise RuntimeError('file changed since proposal: ' + str(p))
        # Snapshot both states to disk for redundancy
        ts = time.time()
        snap_pre = self.snapshot_dir / (proposal.pre_sha + '.txt')
        snap_post = self.snapshot_dir / (proposal.post_sha + '.txt')
        if not snap_pre.exists():
            snap_pre.write_text(proposal.pre_text)
        if not snap_post.exists():
            snap_post.write_text(proposal.post_text)
        # Apply atomically via tempfile + rename
        tmp = p.with_suffix(p.suffix + '.swiszcli.tmp')
        tmp.write_text(proposal.post_text)
        tmp.replace(p)
        cur = self._conn.execute(
            'INSERT INTO edits(ts, session_id, project_id, path, pre_sha, post_sha, snapshot_pre, snapshot_post, diff, description) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (ts, session_id, project_id, str(p), proposal.pre_sha, proposal.post_sha,
             str(snap_pre), str(snap_post), proposal.diff, proposal.description),
        )
        self._conn.commit()
        return {'action': 'applied', 'id': cur.lastrowid, 'ts': ts, 'lines_changed': len(proposal.diff.splitlines())}

    def undo(self, edit_id=None):
        """Revert a specific edit (or the most recent unreverted one if id is None)."""
        if edit_id is None:
            row = self._conn.execute(
                'SELECT * FROM edits WHERE reverted = 0 ORDER BY ts DESC LIMIT 1'
            ).fetchone()
        else:
            row = self._conn.execute('SELECT * FROM edits WHERE id = ?', (edit_id,)).fetchone()
        if not row:
            return {'action': 'noop', 'reason': 'no edit to undo'}
        if row['reverted']:
            return {'action': 'noop', 'reason': 'already reverted', 'id': row['id']}
        p = Path(row['path'])
        if not p.exists():
            raise FileNotFoundError('target file gone: ' + row['path'])
        current = p.read_text()
        # Sanity: current should match post_sha (file hasn't been further edited outside)
        if _sha(current) != row['post_sha']:
            # Still allow but warn
            warning = 'file modified outside swiszcli since this edit; current sha=' + _sha(current) + ' expected=' + row['post_sha']
        else:
            warning = ''
        snap_pre = Path(row['snapshot_pre'])
        if not snap_pre.exists():
            raise FileNotFoundError('snapshot missing: ' + row['snapshot_pre'])
        restored = snap_pre.read_text()
        # Apply via tempfile + rename
        tmp = p.with_suffix(p.suffix + '.swiszcli.tmp')
        tmp.write_text(restored)
        tmp.replace(p)
        self._conn.execute(
            'UPDATE edits SET reverted = 1, reverted_at = ? WHERE id = ?',
            (time.time(), row['id']),
        )
        self._conn.commit()
        return {'action': 'reverted', 'id': row['id'], 'path': row['path'], 'warning': warning}

    def history(self, path=None, limit=20):
        if path:
            rows = self._conn.execute(
                'SELECT * FROM edits WHERE path = ? ORDER BY ts DESC LIMIT ?',
                (str(path), limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                'SELECT * FROM edits ORDER BY ts DESC LIMIT ?', (limit,)
            ).fetchall()
        return [{
            'id': r['id'], 'ts': r['ts'], 'path': r['path'],
            'pre_sha': r['pre_sha'], 'post_sha': r['post_sha'],
            'description': r['description'], 'reverted': bool(r['reverted']),
            'lines_changed': len(r['diff'].splitlines()),
        } for r in rows]

