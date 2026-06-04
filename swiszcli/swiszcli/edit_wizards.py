"""Edit + AST wizards: DSL handlers for LLM-driven structural editing."""
from __future__ import annotations
import json
import re
import shlex

class EditOps:
    def __init__(self, edit_engine, ast_index, session_id='', project_id=''):
        self.eng = edit_engine
        self.idx = ast_index
        self.session_id = session_id
        self.project_id = project_id
        self._pending = None  # last proposal awaiting confirm

    # ---- AST index ops --------------------------------------------------
    def find_symbol(self, name):
        hits = self.idx.find_symbol(name.strip(), project_id=self.project_id or None)
        if not hits:
            return 'no symbol named ' + name + ' in index'
        out = ['symbol: ' + name + ' (' + str(len(hits)) + ' hit(s))']
        for h in hits[:20]:
            out.append('  ' + h['kind'] + ' ' + h['qualname'] + ' at ' + h['path'] + ':' + str(h['lineno']) + '-' + str(h['end_lineno']))
            if h.get('docstring'):
                out.append('    ""' + '"' + h['docstring'][:120].replace(chr(10), ' ') + '""' + '"')
        return chr(10).join(out)

    def find_in_file(self, path):
        hits = self.idx.find_in_file(path.strip())
        if not hits:
            return 'no indexed symbols in ' + path
        out = ['symbols in ' + path + ' (' + str(len(hits)) + '):']
        for h in hits:
            out.append('  ' + h['kind'] + ' ' + h['qualname'] + ' line ' + str(h['lineno']) + '-' + str(h['end_lineno']))
        return chr(10).join(out)

    def reindex(self, root, project_id=None):
        pid = project_id or self.project_id or ''
        res = self.idx.index_project(root, project_id=pid)
        return 'indexed: ' + str(res['indexed']) + ' files, unchanged: ' + str(res['unchanged']) + ', errors: ' + str(res['errors'])

    # ---- edit ops -------------------------------------------------------
    def propose_replace(self, path, old, new, description=''):
        prop = self.eng.propose_replace(path, old, new, description=description)
        if prop.is_noop():
            return 'noop: new_text equals old_text'
        self._pending = prop
        return ('proposal staged for ' + path + chr(10) + '--- diff preview ---' + chr(10) + prop.render_preview(120) + chr(10) + '--- end diff ---' + chr(10) + 'emit: edit apply  -- to apply, or edit cancel to drop')

    def propose_ast_replace(self, path, symbol, new_source, description=''):
        prop = self.eng.propose_ast_replace(path, symbol, new_source, description=description)
        if prop.is_noop():
            return 'noop: new_source equals existing'
        self._pending = prop
        return ('ast proposal staged for ' + path + ' (' + symbol + ')' + chr(10) + '--- diff preview ---' + chr(10) + prop.render_preview(120) + chr(10) + '--- end diff ---' + chr(10) + 'emit: edit apply  -- to apply, or edit cancel to drop')

    def apply_pending(self):
        if not self._pending:
            return 'no pending proposal'
        try:
            res = self.eng.apply(self._pending, session_id=self.session_id, project_id=self.project_id)
        finally:
            applied_proposal = self._pending
            self._pending = None
        # reindex the file we just touched (if it's python)
        try:
            if applied_proposal.path.endswith('.py'):
                self.idx.index_file(applied_proposal.path, project_id=self.project_id)
        except Exception:
            pass
        return 'applied edit id=' + str(res.get('id')) + ' (' + str(res.get('lines_changed', 0)) + ' diff lines)'

    def cancel_pending(self):
        if not self._pending:
            return 'no pending proposal'
        self._pending = None
        return 'pending proposal dropped'

    def undo(self, edit_id=None):
        res = self.eng.undo(edit_id)
        if res['action'] == 'noop':
            return 'undo noop: ' + res.get('reason', '')
        out = 'reverted edit id=' + str(res['id']) + ' on ' + res['path']
        if res.get('warning'):
            out += chr(10) + 'WARNING: ' + res['warning']
        # reindex
        try:
            if res['path'].endswith('.py'):
                self.idx.index_file(res['path'], project_id=self.project_id)
        except Exception:
            pass
        return out

    def history(self, path=None, limit=20):
        rows = self.eng.history(path=path, limit=limit)
        if not rows:
            return 'no edits'
        out = ['edit history (' + str(len(rows)) + ' rows):']
        for r in rows:
            flag = '[REVERTED]' if r['reverted'] else '         '
            out.append('  id=' + str(r['id']) + ' ' + flag + ' ' + r['path'] + ' (' + str(r['lines_changed']) + ' lines) -- ' + (r['description'] or ''))
        return chr(10).join(out)

# ---- DSL dispatcher ----------------------------------------------------
_VERBS = ('edit ', 'find symbol ', 'find symbols in ', 'index project')

def _dsl_match(task):
    t = task.strip()
    for v in _VERBS:
        if t == v.rstrip() or t.startswith(v):
            return True
    return False

def dispatch(ops, task):
    """Try to handle an edit/AST verb. Return string result or None to pass through."""
    t = task.strip()
    if not _dsl_match(t):
        return None
    try:
        if t == 'edit apply':
            return ops.apply_pending()
        if t == 'edit cancel':
            return ops.cancel_pending()
        if t == 'edit undo':
            return ops.undo()
        m = re.match(r'^edit undo (\d+)$', t)
        if m:
            return ops.undo(int(m.group(1)))
        m = re.match(r'^edit history(?: (.+))?$', t)
        if m:
            return ops.history(path=m.group(1))
        m = re.match(r'^edit replace (\S+) "(.*?)" with "(.*)"(?: -- (.*))?$', t, re.DOTALL)
        if m:
            return ops.propose_replace(m.group(1), m.group(2), m.group(3), description=(m.group(4) or '').strip())
        m = re.match(r'^edit func (\S+) (\S+) with:(.*)$', t, re.DOTALL)
        if m:
            return ops.propose_ast_replace(m.group(1), m.group(2), m.group(3).lstrip(chr(10)), description='ast rewrite ' + m.group(2))
        m = re.match(r'^find symbol (.+)$', t)
        if m:
            return ops.find_symbol(m.group(1).strip())
        m = re.match(r'^find symbols in (.+)$', t)
        if m:
            return ops.find_in_file(m.group(1).strip())
        m = re.match(r'^index project(?: (.+))?$', t)
        if m:
            return ops.reindex(m.group(1) or '.', project_id=None)
        return 'edit DSL parse error: ' + t
    except FileNotFoundError as e:
        return 'ERROR: ' + str(e)
    except ValueError as e:
        return 'ERROR: ' + str(e)
    except Exception as e:
        return 'ERROR: ' + type(e).__name__ + ': ' + str(e)

