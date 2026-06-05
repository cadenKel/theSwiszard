"""Project state: per-project workspace at ~/.swiszcli/projects/<id>/."""
from __future__ import annotations
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECTS_ROOT = Path.home() / '.swiszcli' / 'projects'

def detect_project(cwd=None):
    cwd = Path(cwd or Path.cwd()).resolve()
    for parent in [cwd] + list(cwd.parents):
        if (parent / '.git').exists():
            name = parent.name
            pid = re.sub(r'[^a-zA-Z0-9_-]', '_', name).lower()[:48] or 'unnamed'
            return {'id': pid, 'name': name, 'root': str(parent)}
    return None

@dataclass
class ProjectState:
    id: str
    name: str
    root: str
    state_dir: Path
    goals: str = ''
    recent_commits: list = field(default_factory=list)
    open_files: list = field(default_factory=list)
    last_session: dict = field(default_factory=dict)
    sessions_count: int = 0

    def render(self):
        out = ['<project_state>']
        out.append('Project: ' + self.name + ' (id=' + self.id + ')')
        out.append('Root: ' + self.root)
        if self.goals.strip():
            out.append('Goals:')
            for line in self.goals.strip().splitlines()[:20]:
                out.append('  ' + line)
        if self.recent_commits:
            out.append('Recent commits:')
            for c in self.recent_commits[:6]:
                out.append('  ' + c.get('sha', '')[:8] + ' ' + c.get('subject', '')[:120])
        if self.open_files:
            out.append('Recently touched files:')
            for f in self.open_files[:8]:
                out.append('  ' + f)
        if self.last_session:
            ls = self.last_session
            out.append('Last session ended ' + _fmt_ago(ls.get('ts', 0)) + ' ago, ' + str(ls.get('turns', 0)) + ' turns, summary: ' + (ls.get('summary', '')[:200] or '(none)'))
        out.append('Sessions in this project: ' + str(self.sessions_count))
        out.append('</project_state>')
        return chr(10).join(out)

def _fmt_ago(ts):
    if not ts:
        return 'unknown'
    dt = time.time() - ts
    if dt < 3600:
        return str(int(dt / 60)) + 'm'
    if dt < 86400:
        return str(int(dt / 3600)) + 'h'
    return str(int(dt / 86400)) + 'd'

def _git_recent_commits(root, n=10):
    try:
        out = subprocess.check_output(
            ['git', '-C', str(root), 'log', '-n', str(n), '--pretty=format:%H\t%s'],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode()
        commits = []
        for line in out.splitlines():
            parts = line.split('\t', 1)
            if len(parts) == 2:
                commits.append({'sha': parts[0], 'subject': parts[1]})
        return commits
    except Exception:
        return []

def _git_recent_files(root, n=8):
    try:
        out = subprocess.check_output(
            ['git', '-C', str(root), 'log', '--name-only', '--pretty=format:', '-n', str(n)],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode()
        files = []
        for line in out.splitlines():
            line = line.strip()
            if line and line not in files:
                files.append(line)
        return files[:n]
    except Exception:
        return []

class ProjectStore:
    def __init__(self, projects_root=None):
        self.root = Path(projects_root or PROJECTS_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, cwd=None):
        det = detect_project(cwd)
        if not det:
            return None
        state_dir = self.root / det['id']
        state_dir.mkdir(parents=True, exist_ok=True)
        goals_file = state_dir / 'goals.md'
        goals = goals_file.read_text() if goals_file.exists() else ''
        sessions_file = state_dir / 'sessions.jsonl'
        last_session = {}
        sessions_count = 0
        if sessions_file.exists():
            for line in sessions_file.read_text().splitlines():
                try:
                    last_session = json.loads(line)
                    sessions_count += 1
                except Exception:
                    continue
        return ProjectState(
            id=det['id'], name=det['name'], root=det['root'], state_dir=state_dir,
            goals=goals,
            recent_commits=_git_recent_commits(det['root']),
            open_files=_git_recent_files(det['root']),
            last_session=last_session, sessions_count=sessions_count,
        )

    def record_session_end(self, project_id, session_id, turns, summary=''):
        state_dir = self.root / project_id
        state_dir.mkdir(parents=True, exist_ok=True)
        rec = {'ts': time.time(), 'session_id': session_id, 'turns': turns, 'summary': summary[:500]}
        with (state_dir / 'sessions.jsonl').open('a') as f:
            f.write(json.dumps(rec) + chr(10))

    def set_goals(self, project_id, goals_text):
        state_dir = self.root / project_id
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / 'goals.md').write_text(goals_text)

