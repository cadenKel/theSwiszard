"""P2 dream_cycle test."""
import sys, tempfile, time
from pathlib import Path
sys.path.insert(0, "/home/ziggibot/swiszcli")
from swiszcli.context_store import ContextStore
from swiszcli import dream_cycle as dc

print("=" * 60)
print("P2 DREAM CYCLE TEST")
print("=" * 60)

class FakeMem:
    def __init__(self): self.calls = []; self._id = 100
    def remember(self, **kw):
        self._id += 1
        self.calls.append(kw)
        return {"id": self._id}

with tempfile.TemporaryDirectory() as td:
    db = Path(td) / "p2.db"
    store = ContextStore(db_path=db)
    vec = [0.1] * 768
    # P1.13 dedup: linearly-independent vectors so each row survives insert
    def _onehot(i, n=768):
        v = [0.0] * n
        v[i] = 1.0
        return v
    vec1, vec2, vec3, vec_old = _onehot(0), _onehot(1), _onehot(2), _onehot(3)
    s = "sess_p2"
    # Insert 3 chunks; manually crank retrievals on two of them
    cid1 = store.store_chunk(s, "chunk_window", "a recurring fact", vec1)
    cid2 = store.store_chunk(s, "tool_result", "a recurring tool output", vec2)
    cid3 = store.store_chunk(s, "chunk_window", "a low-traffic chunk", vec3)
    store._conn.execute("UPDATE chunks SET retrievals = ? WHERE id = ?", (7, cid1))
    store._conn.execute("UPDATE chunks SET retrievals = ? WHERE id = ?", (5, cid2))
    store._conn.execute("UPDATE chunks SET retrievals = ? WHERE id = ?", (1, cid3))
    store._conn.commit()

    # Insert an old chunk to test prune
    cid_old = store.store_chunk(s, "chunk_window", "old stale", vec_old)
    store._conn.execute("UPDATE chunks SET ts = ? WHERE id = ?", (time.time() - 60 * 86400, cid_old))
    store._conn.commit()

    # Insert a bad example (high losses)
    ex_bad = store.store_example("some bad phrasing", vec, "shell", source="learned", weight=0.5)
    store._conn.execute("UPDATE examples SET wins = 1, losses = 5 WHERE id = ?", (ex_bad,))
    store._conn.commit()

    fake = FakeMem()
    cfg = dc.DreamConfig(promote_threshold=5, prune_days=30, dep_min_losses=3, dep_loss_ratio=2.0)

    print("[1] BEFORE:", store.stats())
    print("[2] DRY RUN")
    rep = dc.run(store, config=cfg, mem_client=fake, dry_run=True, log_path=Path(td)/"dream.log")
    print("    report:", rep.summary())
    assert len(rep.promoted) == 2, f"expected 2 promotable, got {len(rep.promoted)}"
    assert rep.pruned_count == 1, f"expected 1 prunable, got {rep.pruned_count}"
    assert len(rep.deprecated) == 1
    # Nothing actually changed (dry_run)
    assert len(fake.calls) == 0
    assert store.stats()["chunks_promoted"] == 0

    print("[3] LIVE RUN")
    rep2 = dc.run(store, config=cfg, mem_client=fake, dry_run=False, log_path=Path(td)/"dream.log")
    print("    report:", rep2.summary())
    print("    promoted:", rep2.promoted)
    print("    deprecated:", rep2.deprecated)
    assert len(rep2.promoted) == 2
    assert len(fake.calls) == 2
    assert rep2.pruned_count == 1
    assert len(rep2.deprecated) == 1

    print("[4] AFTER:", store.stats())
    s2 = store.stats()
    assert s2["chunks_promoted"] == 2

    # 2nd run should be idempotent: nothing left to promote / prune / deprecate
    print("[5] 2nd run (idempotency)")
    rep3 = dc.run(store, config=cfg, mem_client=fake, dry_run=False, log_path=Path(td)/"dream.log")
    print("    report:", rep3.summary())
    assert len(rep3.promoted) == 0
    assert rep3.pruned_count == 0
    assert len(rep3.deprecated) == 0

    # Verify log file written
    log = (Path(td)/"dream.log").read_text()
    lines_in_log = [l for l in log.splitlines() if l.strip()]
    print(f"[6] Log: {len(lines_in_log)} entries")
    assert len(lines_in_log) == 3

    store.close()

print()
print("=" * 60)
print("P2 DREAM CYCLE TEST PASSED")
print("=" * 60)
