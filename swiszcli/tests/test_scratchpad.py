"""Scratchpad end-to-end test."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "/home/ziggibot/swiszcli")

from swiszcli.scratchpad import ScratchpadStore
from swiszcli.scratchpad_wizards import ScratchpadOps, parse_and_dispatch

print("=" * 60)
print("SCRATCHPAD TEST")
print("=" * 60)

with tempfile.TemporaryDirectory() as td:
    db = Path(td) / "sp.db"
    store = ScratchpadStore(db_path=db)
    ops = ScratchpadOps(store, "test_session")

    # 1. Create a plan via DSL
    handled, out = parse_and_dispatch(
        "plan: refactor user model | read users.py | extract validator | add tests | run pytest",
        ops,
    )
    assert handled, "plan: should be handled"
    assert "PLAN CREATED" in out
    assert "[>] 1. read users.py" in out, out
    print("CREATE OK:")
    print(out)
    print()

    # 2. Observe + done step 1
    parse_and_dispatch("observe: read users.py ## found 3 validators, all inline", ops)
    handled, out = parse_and_dispatch("done: extracted validator interface", ops)
    assert "[x] 1." in out
    assert "[>] 2." in out
    print("AFTER STEP 1:")
    print(out)
    print()

    # 3. Decide + blocker mid-step 2
    parse_and_dispatch("decide: use pydantic ## already a project dep, less code", ops)
    parse_and_dispatch("blocker: pydantic v1 vs v2 differs in validator API", ops)
    handled, out = parse_and_dispatch("scratchpad", ops)
    assert "use pydantic" in out
    assert "pydantic v1 vs v2" in out
    print("MID-STEP 2 (with decide+blocker):")
    print(out)
    print()

    # 4. Insert a new step
    parse_and_dispatch(
        "insert: pin pydantic version in pyproject before extracting validator",
        ops,
    )
    handled, out = parse_and_dispatch("scratchpad", ops)
    assert "pin pydantic version" in out
    print("AFTER INSERT:")
    print(out)
    print()

    # 5. Complete remaining steps
    parse_and_dispatch("done: pinned to 2.x", ops)
    parse_and_dispatch("done: validator extracted to validators.py", ops)
    parse_and_dispatch("done: 6 tests added, all green", ops)
    handled, out = parse_and_dispatch("done: pytest passed", ops)
    assert "COMPLETE" in out, out
    print("FINAL:")
    print(out)
    print()

    # 6. Verify it landed in archives and not active anymore
    active = store.get_active("test_session")
    assert active is None or active.is_done, "should be no active scratchpad after completion"
    archived = store.recent_archived(session_id="test_session")
    assert len(archived) == 1
    assert archived[0].status == "complete"

    # 7. Cross-turn persistence: re-open store from disk
    store.close()
    store2 = ScratchpadStore(db_path=db)
    ops2 = ScratchpadOps(store2, "another_session")
    handled, out = parse_and_dispatch(
        "plan: ship the demo | build it | test it | push it",
        ops2,
    )
    assert "PLAN CREATED" in out

    # Simulate process restart mid-task
    parse_and_dispatch("done: built", ops2)
    store2.close()
    store3 = ScratchpadStore(db_path=db)
    ops3 = ScratchpadOps(store3, "another_session")
    handled, out = parse_and_dispatch("scratchpad", ops3)
    assert "[x] 1. build it" in out
    assert "[>] 2. test it" in out
    print("CROSS-PROCESS PERSISTENCE OK:")
    print(out)
    store3.close()

print()
print("=" * 60)
print("SCRATCHPAD TEST PASSED")
print("=" * 60)
