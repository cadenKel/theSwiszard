"""Tool synthesis: invent new wizards from observed raw shell usage.

When the LLM falls back to raw shell (run `...` swiszard calls), we capture
the (user_text, shell_cmd, success) triple as a chunk with kind=shell_fallback.
The dream_cycle then clusters these chunks and proposes new wizards.
"""
from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_PATTERN_RUN = re.compile(r'^\s*run\s+' + chr(96) + r'(.+?)' + chr(96) + r'\s*$', re.DOTALL)

def extract_shell(task):
    if not task:
        return None
    m = _PATTERN_RUN.match(task)
    if m:
        return m.group(1).strip()
    return None

def shell_signature(cmd):
    if not cmd:
        return ''
    tokens = cmd.split()
    sig = []
    for tok in tokens[:4]:
        if tok.startswith('-'):
            sig.append(tok)
        elif tok.startswith('/') or tok.startswith('~'):
            sig.append('<PATH>')
        elif re.fullmatch(r'\d+', tok) or re.fullmatch(r'[0-9a-f]{8,}', tok):
            sig.append('<ARG>')
        else:
            sig.append(tok)
    return ' '.join(sig)

def suggest_name(signature):
    toks = [t for t in signature.split() if re.fullmatch(r'[A-Za-z][A-Za-z_-]*', t)]
    if not toks:
        return 'wizard_unnamed'
    return '_'.join(toks[:2]).lower().replace('-', '_')

@dataclass
class WizardProposal:
    proposal_id: str
    signature: str
    sample_commands: list = field(default_factory=list)
    sample_user_texts: list = field(default_factory=list)
    occurrences: int = 0
    suggested_name: str = ''
    created_at: float = field(default_factory=time.time)
    def to_dict(self):
        return {
            'proposal_id': self.proposal_id,
            'signature': self.signature,
            'suggested_name': self.suggested_name,
            'occurrences': self.occurrences,
            'sample_commands': self.sample_commands,
            'sample_user_texts': self.sample_user_texts,
            'created_at': self.created_at,
        }

class ToolSynthesizer:
    def __init__(self, store, proposals_dir=None, min_cluster_size=3):
        self.store = store
        self.proposals_dir = Path(proposals_dir) if proposals_dir else (Path.home() / '.swiszcli' / 'proposals')
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self.min_cluster_size = min_cluster_size
    def fetch_shell_fallbacks(self):
        cur = self.store._conn.execute(
            'SELECT id, text FROM chunks WHERE kind = ? AND promoted = 0',
            ('shell_fallback',),
        )
        return [(r['id'], r['text']) for r in cur.fetchall()]
    def parse_fallback(self, text):
        if ' | CMD: ' not in text:
            return None, None
        try:
            u_part, c_part = text.split(' | CMD: ', 1)
            user_text = u_part.replace('USER: ', '', 1).strip()
            cmd = c_part.strip()
            return user_text, cmd
        except Exception:
            return None, None
    def synthesize(self, dry_run=False):
        fallbacks = self.fetch_shell_fallbacks()
        clusters = {}
        for cid, text in fallbacks:
            user_text, cmd = self.parse_fallback(text)
            if not cmd:
                continue
            sig = shell_signature(cmd)
            if not sig:
                continue
            entry = clusters.setdefault(sig, {'chunk_ids': [], 'cmds': [], 'user_texts': []})
            entry['chunk_ids'].append(cid)
            entry['cmds'].append(cmd)
            if user_text:
                entry['user_texts'].append(user_text)
        proposals = []
        for sig, entry in clusters.items():
            if len(entry['chunk_ids']) < self.min_cluster_size:
                continue
            pid = 'prop_' + str(int(time.time() * 1000)) + '_' + suggest_name(sig)
            prop = WizardProposal(
                proposal_id=pid,
                signature=sig,
                sample_commands=entry['cmds'][:5],
                sample_user_texts=entry['user_texts'][:5],
                occurrences=len(entry['chunk_ids']),
                suggested_name=suggest_name(sig),
            )
            proposals.append(prop)
            if not dry_run:
                pf = self.proposals_dir / (pid + '.json')
                pf.write_text(json.dumps(prop.to_dict(), indent=2))
                for cid in entry['chunk_ids']:
                    self.store._conn.execute(
                        'UPDATE chunks SET promoted = 1 WHERE id = ?',
                        (cid,),
                    )
                self.store._conn.commit()
        return proposals
    def list_pending(self):
        return sorted(self.proposals_dir.glob('prop_*.json'))
    def load_proposal(self, proposal_id):
        pf = self.proposals_dir / (proposal_id + '.json')
        if not pf.exists():
            return None
        return json.loads(pf.read_text())
    def approve(self, proposal_id, embed_fn=None):
        prop = self.load_proposal(proposal_id)
        if not prop:
            raise FileNotFoundError('no such proposal: ' + proposal_id)
        wizard_name = prop['suggested_name']
        seeded = 0
        if embed_fn:
            for txt in prop['sample_user_texts']:
                try:
                    vec = embed_fn(txt)
                    self.store.store_example(
                        text=txt,
                        embedding=vec,
                        wizard_name=wizard_name,
                        source='synthesized',
                        weight=0.7,
                    )
                    seeded += 1
                except Exception:
                    continue
        pf = self.proposals_dir / (proposal_id + '.json')
        approved_dir = self.proposals_dir / 'approved'
        approved_dir.mkdir(parents=True, exist_ok=True)
        pf.rename(approved_dir / pf.name)
        return {
            'wizard_name': wizard_name,
            'seeded_examples': seeded,
            'signature': prop['signature'],
        }
    def reject(self, proposal_id):
        pf = self.proposals_dir / (proposal_id + '.json')
        rejected_dir = self.proposals_dir / 'rejected'
        rejected_dir.mkdir(parents=True, exist_ok=True)
        pf.rename(rejected_dir / pf.name)
        return {'rejected': proposal_id}

def capture_shell_fallback(store, session_id, embed_fn, user_text, shell_cmd):
    text = 'USER: ' + (user_text or '') + ' | CMD: ' + (shell_cmd or '')
    try:
        vec = embed_fn(text)
    except Exception:
        vec = [0.0] * 768
    store.store_chunk(session_id, 'shell_fallback', text, vec)
