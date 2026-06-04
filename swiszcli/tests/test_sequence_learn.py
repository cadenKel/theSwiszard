"""Sequence learning test."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "/home/ziggibot/swiszcli")
from swiszcli.context_store import ContextStore
from swiszcli.sequence_learn import SequenceStore, render_sequence_hint

def stub_embed(text):
    import random
    random.seed(sum(ord(c) for c in text[:64]))
    return [random.random() for _ in range(768)]

print("=" * 60)
print("SEQUENCE LEARNING TEST")
print("=" * 60)

with tempfile.TemporaryDirectory() as td:
    store = ContextStore(db_path=Path(td) / "ctx.db")
    seq = SequenceStore(store._conn)
    # Single-step sequence: ignored
    r = seq.record("just read a file", stub_embed("just read"), [{"wizard": "read", "task": "read /tmp/x"}])
    assert r is None
    # Multi-step recipe
    steps = [
        {"wizard": "find", "task": "find files matching TODO in /tmp/proj"},
        {"wizard": "grep", "task": "grep TODO in /tmp/proj"},
        {"wizard": "issue", "task": "github create issue from TODO"},
    ]
    r = seq.record("turn TODOs into github issues", stub_embed("turn TODOs into github issues"), steps)
    assert r["action"] == "learn"
    sid1 = r["id"]
    print("learned:", sid1, "steps", len(r["steps"]))
    # Reinforce: same input + same steps
    r2 = seq.record("turn TODOs into github issues", stub_embed("turn TODOs into github issues"), steps)
    assert r2["action"] == "reinforce", f"got {r2}"
    assert r2["id"] == sid1
    print("reinforce ok, count =", seq.count())
    assert seq.count() == 1
    # Recall by similar text
    matches = seq.find(stub_embed("turn TODOs into github issues"), top_k=3, min_score=0.5)
    assert matches and matches[0].id == sid1
    hint = render_sequence_hint(matches)
    assert "<sequence_hint>" in hint
    assert "step 1" in hint and "step 3" in hint
    print(hint)
    store.close()

print()
print("=" * 60)
print("SEQUENCE LEARNING TEST PASSED")
print("=" * 60)
