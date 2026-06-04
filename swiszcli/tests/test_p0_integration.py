"""P0 end-to-end integration test."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, '/home/ziggibot/swiszcli')
from swiszcli.context_store import ContextStore
from swiszcli.router import Router
from swiszcli.chunks import ChunkCapture, make_recall_fn, render_chunks
from swiszcli.router_hint import compose_extra_system, router_hint

print('=' * 60)
print('P0 END-TO-END INTEGRATION TEST')
print('=' * 60)

with tempfile.TemporaryDirectory() as td:
    db = Path(td) / 'p0.db'
    store = ContextStore(db_path=db)
    router = Router(store)
    capture = ChunkCapture(store=store, window_size=8)

    print('\n[1] Seed handler prototypes')
    written = router.seed()
    print('    wrote {} seed examples, total in db: {}'.format(written, store.count_examples()))
    assert written > 0

    print('\n[2] Simulating a 10-turn session')
    convo = [
        ('user', 'lets read the agent.py file'),
        ('assistant', 'sure, ill open it now'),
        ('user', 'grep for SWISZ in protocol.py'),
        ('assistant', 'found three matches'),
        ('user', 'now find all the py files under swiszcli'),
        ('assistant', 'found 23 files'),
        ('user', 'search the web for nomic-embed-text token limit'),
        ('assistant', 'the token limit is 8192'),
        ('user', 'save that as a fact: nomic-embed has 8192 token limit'),
        ('assistant', 'saved to swizmem'),
    ]
    for role, text in convo:
        capture.record_turn(role, text)
    capture.record_tool_result('grep SWISZ in /path/protocol.py', 'match line 17\nmatch line 42')
    print('    chunks in db after session: {}'.format(store.count_chunks()))
    assert store.count_chunks() >= 1

    print('\n[3] Recall test: new user asks about agent.py')
    recall = make_recall_fn(store, capture)
    hits = recall('what was that thing about reading agent.py')
    rendered = render_chunks(hits)
    print('    {} hits returned'.format(len(hits)))
    for h in hits:
        if '_error' not in h:
            kind = h['kind']
            sc = h['score']
            tx = h['text'][:80]
            print('      [{} score={:.2f}] {}...'.format(kind, sc, tx))
    assert len(hits) >= 1

    print('\n[4] render_chunks preview:')
    for line in rendered.split(chr(10))[:6]:
        print('    ' + line)

    print('\n[5] Router hint for: show me the contents of agent.py')
    d = router.decide('show me the contents of agent.py')
    print('    mode={} wizard={} score={:.3f}'.format(d.mode, d.wizard_name, d.score))
    hint = router_hint(d)
    if hint:
        print('    HINT preview:')
        for line in hint.split(chr(10))[:3]:
            print('      ' + line)

    print('\n[6] Composed extra_system block:')
    composed = compose_extra_system(router, 'show me agent.py', rendered)
    print('    {} chars composed'.format(len(composed)))
    assert 'router_hint' in composed or 'recalled_context' in composed

    print('\n[7] Closing session — writing session_frame')
    before = store.count_chunks()
    capture.close_session()
    after = store.count_chunks()
    print('    chunks: {} -> {}'.format(before, after))
    assert after > before

    print('\n[8] Retrieval counter increments')
    hits1 = recall('agent.py reading')
    hits2 = recall('agent.py reading')
    if hits1 and hits2:
        r1 = hits1[0]['retrievals']
        r2 = hits2[0]['retrievals']
        print('    retrievals {} -> {}'.format(r1, r2))
        assert r2 > r1

    store.close()

print()
print('=' * 60)
print('P0 INTEGRATION TEST PASSED')
print('=' * 60)
