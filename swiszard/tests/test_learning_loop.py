import time
import sqlite3
import requests

BASE = "http://localhost:8765"
DB = "/home/ziggibot/.hermes/swiszard/routes.db"
NOVEL_TASK = "xyzzy_novel_task_probe_" + str(int(time.time()))


def get_counts(handler):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT SUM(success_count), SUM(fail_count) FROM examples WHERE handler=?", (handler,)).fetchone()
    return (row[0] or 0, row[1] or 0)


def test_loop_end_to_end():
    # Step 1: novel task should not crash
    r = requests.get(f"{BASE}/router/match", params={"task": NOVEL_TASK})
    assert r.status_code == 200
    first_handler = r.json().get("handler")

    # Step 2: give feedback
    target = "handler_shell"
    wins_before, losses_before = get_counts(target)
    from swiszard.router import swiszard_feedback
    swiszard_feedback(NOVEL_TASK, target, True)
    wins_after, _ = get_counts(target)
    assert wins_after > wins_before, "feedback did not increment success_count"

    # Step 3: same task should now route to handler_shell
    r2 = requests.get(f"{BASE}/router/match", params={"task": NOVEL_TASK})
    assert r2.status_code == 200
    assert r2.json().get("handler") == target
