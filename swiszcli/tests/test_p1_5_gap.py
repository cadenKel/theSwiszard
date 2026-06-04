"""P1.5 gap detector + research wizard wiring test (no live searxng)."""
import sys
sys.path.insert(0, '/home/ziggibot/swiszcli')
from swiszcli.gap_detector import detect, hint_block
from swiszcli.research_wizard import research

print('=' * 60)
print('P1.5 GAP DETECTOR + RESEARCH WIZARD TEST')
print('=' * 60)

# 1. Clean draft — no gap
draft_clean = "The capital of France is Paris."
v = detect(draft_clean)
print(f'[1] Clean draft: has_gap={v.has_gap} ({v.summary})')
assert not v.has_gap

# 2. Hedge draft — should flag
draft_hedge = "I think the latest version of qwen is probably 3.5 but I am not sure."
v = detect(draft_hedge)
print(f'[2] Hedge draft: has_gap={v.has_gap} ({v.summary})')
print(f'    queries: {v.research_queries}')
assert v.has_gap
assert len(v.hedge_hits) >= 2
assert len(v.research_queries) >= 1

# 3. External claim — should flag
draft_ext = "Python version 3.12 was released in 2023."
v = detect(draft_ext)
print(f'[3] External draft: has_gap={v.has_gap} ({v.summary})')
assert v.has_gap

# 4. Fabrication — should flag
draft_fab = "For example, let's say the API returns hypothetically a JSON object."
v = detect(draft_fab)
print(f'[4] Fab draft: has_gap={v.has_gap} ({v.summary})')
assert v.has_gap

# 5. hint_block formatting
v = detect("I think foo. As of my training the latest is bar.")
hb = hint_block(v)
print(f'[5] hint_block ({len(hb)} chars):')
for line in hb.split(chr(10))[:6]:
    print(f'    {line}')
assert '<gap_detector>' in hb

# 6. research wizard with stub swiszard_do (no real searxng)
calls = []
def fake_sd(task):
    calls.append(task)
    return f'stub result for: {task}'
ev = research(
    queries=['what is the latest qwen', 'python release date'],
    swiszard_do=fake_sd,
)
print(f'[6] Research wizard: {len(calls)} swiszard calls, {len(ev)} chars evidence')
print(f'    swiszard calls: {calls}')
assert len(calls) == 2
assert '<research_context>' in ev
assert 'stub result' in ev

# 7. research with mem recall stub
def fake_mem(q, top_k=3):
    return [{'id': 42, 'body': f'remembered fact about {q}'}]
ev2 = research(
    queries=['something'],
    swiszard_do=fake_sd,
    mem_recall_triggers=fake_mem,
)
print(f'[7] With mem stub: {len(ev2)} chars')
assert 'mem[42]' in ev2

print()
print('=' * 60)
print('P1.5 TEST PASSED')
print('=' * 60)
