"""Tool synthesis test."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, '/home/ziggibot/swiszcli')
from swiszcli.context_store import ContextStore
from swiszcli.tool_synthesis import (
    ToolSynthesizer, extract_shell, shell_signature,
    suggest_name, capture_shell_fallback,
)

print('=' * 60)
print('TOOL SYNTHESIS TEST')
print('=' * 60)

BT = chr(96)
assert extract_shell('run ' + BT + 'git status' + BT) == 'git status'
assert extract_shell('read /tmp/x') is None
assert shell_signature('git status -s') == 'git status -s'
assert shell_signature('lsof -i :8080') == 'lsof -i :8080'
assert suggest_name('git status -s') == 'git_status'
print('parsers OK')

with tempfile.TemporaryDirectory() as td:
    db = Path(td) / 'ctx.db'
    propdir = Path(td) / 'proposals'
    store = ContextStore(db_path=db)
    def stub_embed(text):
        h = sum(ord(c) for c in text[:64])
        import random
        random.seed(h)
        return [random.random() for _ in range(768)]
    for ut, cmd in [
        ('check git changes', 'git status'),
        ('see what is dirty', 'git status -s'),
        ('git status please', 'git status'),
        ('any uncommitted?', 'git status'),
        ('what is in port 8080', 'lsof -i :8080'),
        ('find port 3000 owner', 'lsof -i :3000'),
    ]:
        capture_shell_fallback(store, 'sess1', stub_embed, ut, cmd)
    synth = ToolSynthesizer(store, proposals_dir=propdir, min_cluster_size=3)
    proposals = synth.synthesize()
    print('proposals:', [(p.suggested_name, p.occurrences) for p in proposals])
    assert len(proposals) == 1, 'expected git_status cluster only'
    p0 = proposals[0]
    assert p0.suggested_name == 'git_status'
    assert p0.occurrences == 3
    proposals2 = synth.synthesize()
    assert len(proposals2) == 0, 'idempotent: marked promoted'
    pending = synth.list_pending()
    assert len(pending) == 1
    result = synth.approve(p0.proposal_id, embed_fn=stub_embed)
    print('approved:', result)
    assert result['seeded_examples'] == 3
    assert (propdir / 'approved').exists()
    assert not (propdir / (p0.proposal_id + '.json')).exists()
    examples = store._conn.execute(
        "SELECT text, wizard_name, source FROM examples WHERE source='synthesized'"
    ).fetchall()
    assert len(examples) == 3
    assert all(e['wizard_name'] == 'git_status' for e in examples)
    print('approve flow OK, 4 seed examples added under wizard git_status')
    store.close()

print()
print('=' * 60)
print('TOOL SYNTHESIS TEST PASSED')
print('=' * 60)
