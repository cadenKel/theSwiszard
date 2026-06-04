"""P1 learner end-to-end."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, '/home/ziggibot/swiszcli')

from swiszcli.context_store import ContextStore
from swiszcli.router import Router
from swiszcli.learn import Learner

print('=' * 60)
print('P1 LEARNER TEST')
print('=' * 60)

with tempfile.TemporaryDirectory() as td:
    db = Path(td) / 'p1.db'
    store = ContextStore(db_path=db)
    router = Router(store)
    learner = Learner(store)

    n = router.seed()
    print(f'[1] Seeded {n} examples')

    novel = "list every python file under the swiszcli folder"
    d = router.decide(novel)
    print(f'[2] Pre-learn for {novel!r}: mode={d.mode} wiz={d.wizard_name} score={d.score:.3f}')
    pre_score = d.score

    sw_task = "find files matching *.py in /home/ziggibot/swiszcli/swiszcli"
    result = learner.observe(novel, sw_task, success=True)
    print(f'[3] Learner: {result}')
    assert result['action'] == 'learn'
    assert result['wizard'] == 'find_files'

    d2 = router.decide(novel)
    print(f'[4] Post-learn: mode={d2.mode} wiz={d2.wizard_name} score={d2.score:.3f}')
    assert d2.wizard_name == 'find_files', f'got {d2.wizard_name}'
    assert d2.score >= pre_score

    para = "show me all .py files in swiszcli"
    d3 = router.decide(para)
    print(f'[5] Paraphrase: mode={d3.mode} wiz={d3.wizard_name} score={d3.score:.3f}')

    n_before = store.count_examples()
    r2 = learner.observe(novel, sw_task, success=True)
    n_after = store.count_examples()
    print(f'[6] Re-observe: {r2} ({n_before} -> {n_after} examples)')
    assert r2['action'] == 'reinforce'
    assert n_after == n_before, 'dedup failed'

    bad = "make me a sandwich"
    r3 = learner.observe(bad, "uhhh this is not a swiszard task", success=True)
    print(f'[7] Unrecognized task: {r3}')
    assert r3['action'] == 'skip'

    store.close()

print()
print('=' * 60)
print('P1 LEARNER TEST PASSED')
print('=' * 60)
