"""Sequence learning: capture wizard sequences from a single assistant turn."""
from __future__ import annotations
import json
import sqlite3
import time
from dataclasses import dataclass, field

SCHEMA = """
CREATE TABLE IF NOT EXISTS sequences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT    NOT NULL,
    embedding   BLOB    NOT NULL,
    steps_json  TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'observed',
    weight      REAL    NOT NULL DEFAULT 1.0,
    wins        INTEGER NOT NULL DEFAULT 0,
    losses      INTEGER NOT NULL DEFAULT 0,
    occurrences INTEGER NOT NULL DEFAULT 1,
    last_used   REAL    NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seq_source ON sequences(source);
"""

import array
import struct

def _pack(vec):
    return struct.pack('<' + 'f' * len(vec), *vec)

def _unpack(blob, dim=768):
    return list(struct.unpack('<' + 'f' * dim, blob))

def _cosine(a, b):
    s = sum(x*y for x, y in zip(a, b))
    na = sum(x*x for x in a) ** 0.5
    nb = sum(x*x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return s / (na * nb)

@dataclass
class SequenceMatch:
    id: int
    text: str
    steps: list
    score: float
    weight: float
    wins: int
    losses: int

class SequenceStore:
    def __init__(self, conn):
        # Reuse the existing ContextStore connection
        self._conn = conn
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def record(self, text, embedding, steps, source='observed'):
        if len(steps) < 2:
            return None
        # Dedup: if a high-cosine match with the same exact step sequence exists,
        # increment occurrences + win count instead of inserting.
        existing = self.find(embedding, top_k=1, min_score=0.92)
        if existing:
            best = existing[0]
            if best.steps == steps:
                self._conn.execute(
                    'UPDATE sequences SET occurrences = occurrences + 1, '
                    'wins = wins + 1, last_used = ? WHERE id = ?',
                    (time.time(), best.id),
                )
                self._conn.commit()
                return {'action': 'reinforce', 'id': best.id, 'steps': steps}
        now = time.time()
        cur = self._conn.execute(
            'INSERT INTO sequences(text, embedding, steps_json, source, created_at, last_used) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (text, _pack(embedding), json.dumps(steps), source, now, now),
        )
        self._conn.commit()
        return {'action': 'learn', 'id': cur.lastrowid, 'steps': steps}

    def find(self, embedding, top_k=3, min_score=0.6):
        rows = self._conn.execute('SELECT * FROM sequences').fetchall()
        out = []
        for r in rows:
            vec = _unpack(r['embedding'])
            score = _cosine(embedding, vec)
            if score < min_score:
                continue
            out.append(SequenceMatch(
                id=r['id'], text=r['text'], steps=json.loads(r['steps_json']),
                score=score, weight=r['weight'], wins=r['wins'], losses=r['losses'],
            ))
        out.sort(key=lambda m: -m.score)
        return out[:top_k]

    def reinforce(self, sid, win=True):
        col = 'wins' if win else 'losses'
        self._conn.execute(
            f'UPDATE sequences SET {col} = {col} + 1, last_used = ? WHERE id = ?',
            (time.time(), sid),
        )
        self._conn.commit()
        # Delegate to WeightEngine for unified cross-signal compositing
        try:
            from .weight_engine import get_engine as _get_eng
            _get_eng().observe_seq(sid, 1.0 if win else 0.0)
            _get_eng().save()
        except Exception:
            pass  # WeightEngine is additive; never break sequence_learn

    def count(self):
        return self._conn.execute('SELECT COUNT(*) c FROM sequences').fetchone()['c']

def render_sequence_hint(matches):
    """Compose a <sequence_hint> XML-ish block for the LLM system prompt."""
    if not matches:
        return ''
    lines = ['<sequence_hint>']
    lines.append('Past similar inputs led to this multi-step recipe:')
    top = matches[0]
    lines.append('  trigger (cosine ' + format(top.score, '.2f') + '): ' + top.text[:120])
    for i, step in enumerate(top.steps, 1):
        lines.append('  step ' + str(i) + ': wizard=' + step.get('wizard', '?') + '  task: ' + (step.get('task') or '')[:120])
    lines.append('Emit these as swiszard calls in this order unless context demands otherwise.')
    lines.append('</sequence_hint>')
    return chr(10).join(lines)
