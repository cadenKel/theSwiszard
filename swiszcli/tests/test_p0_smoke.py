"""P0 smoke test — verify the foundation actually works."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, '/home/ziggibot/swiszcli')

from swiszcli.embed import embed, EmbedError
from swiszcli.context_store import ContextStore
from swiszcli.router import Router, HANDLER_SEEDS

print("=" * 60)
print("P0 smoke test")
print("=" * 60)

# 1. Embedding works
print("\n[1] embed() against local ollama nomic-embed-text...")
try:
    v = embed("hello world")
    assert isinstance(v, list) and len(v) > 100, f"bad vector: len={len(v)}"
    print(f"    OK — vector dim {len(v)}")
except EmbedError as e:
    print(f"    FAIL: {e}")
    sys.exit(1)

# 2. ContextStore round-trip
print("\n[2] ContextStore — store + recall chunks...")
with tempfile.TemporaryDirectory() as td:
    db = Path(td) / "test.db"
    store = ContextStore(db_path=db)

    sid = "session-A"
    e1 = embed("the wizard pattern is interview-driven")
    e2 = embed("sean prefers minimal execution")
    e3 = embed("python files live under swiszcli/")

    cid1 = store.store_chunk(sid, "chunk_window", "the wizard pattern is interview-driven", e1)
    cid2 = store.store_chunk(sid, "chunk_window", "sean prefers minimal execution", e2)
    cid3 = store.store_chunk("session-B", "session_frame", "python files live under swiszcli/", e3)
    print(f"    stored chunk ids: {cid1}, {cid2}, {cid3}")

    q = embed("what about the interview wizard?")
    hits = store.recall_chunks(q, top_k=3, session_id=sid)
    print(f"    recall returned {len(hits)} hits:")
    for h in hits:
        print(f"      id={h['id']} kind={h['kind']} score={h['score']:.3f} :: {h['text'][:50]}")
    assert len(hits) >= 1, "expected at least one recall hit"
    assert hits[0]["id"] == cid1, "expected wizard-pattern chunk on top"

    # session_frame should be recallable cross-session
    q2 = embed("where do the python files go")
    hits2 = store.recall_chunks(q2, top_k=3, session_id=sid)
    cross = [h for h in hits2 if h["session_id"] == "session-B"]
    print(f"    cross-session via session_frame: {len(cross)} hit(s)")
    assert len(cross) >= 1, "session_frame should cross sessions"
    store.close()
print("    OK")

# 3. Router seed + match
print("\n[3] Router — seed examples + cosine match...")
with tempfile.TemporaryDirectory() as td:
    db = Path(td) / "test.db"
    store = ContextStore(db_path=db)
    router = Router(store)
    written = router.seed()
    expected = sum(len(v) for v in HANDLER_SEEDS.values())
    print(f"    seeded {written}/{expected} examples")
    assert written == expected, f"seed mismatch: {written} vs {expected}"

    test_cases = [
        ("show me agent.py", "read"),
        ("grep TODO in /tmp", "grep"),
        ("look up the latest qwen release", "research"),
        ("save this fact: sean likes coffee", "remember"),
        ("what do you remember about libbie", "recall"),
        ("find files matching *.py in src", "find_files"),
    ]
    correct = 0
    for inp, expected_wizard in test_cases:
        d = router.decide(inp)
        marker = "OK" if d.wizard_name == expected_wizard else "MISS"
        if d.wizard_name == expected_wizard:
            correct += 1
        print(f"    [{marker}] {inp!r}")
        print(f"          -> wizard={d.wizard_name} mode={d.mode} score={d.score:.3f}")
        print(f"          matched: {d.matched_text!r}")

    print(f"\n    router accuracy: {correct}/{len(test_cases)}")
    assert correct >= len(test_cases) * 0.6, "router below 60% on seed paraphrases"
    store.close()
print("    OK")

print("\n" + "=" * 60)
print("P0 smoke test PASSED")
print("=" * 60)
