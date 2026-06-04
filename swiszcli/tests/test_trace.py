"""Trace logger + nested parent linkage tests."""
from __future__ import annotations
import pytest
from swiszcli import pools, trace as tracelog
from swiszcli.script_runner import ScriptRunner
from swiszcli.wizard import Cancelled, Choice, REGISTRY, Step, Wizard, register


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    pools.set_default_db(tmp_path / "pools.db")
    tw = tracelog.TraceWriter(tmp_path / "traces.db")
    tracelog.set_default(tw)
    REGISTRY.clear()
    yield tw
    tracelog.set_default(None)  # type: ignore[arg-type]
    tw.close()


def test_writer_lifecycle():
    tw = tracelog.get_default()
    tid = tw.start("foo.wiz", "user", initial_ctx={"x": 1})
    row = tw.get(tid)
    assert row["status"] == "running"
    tw.end(tid, {"x": 1, "y": 2}, {"r": "ok"}, "ok")
    row = tw.get(tid)
    assert row["status"] == "ok"
    assert row["ended_at"] is not None


def test_run_writes_trace_row():
    pools.get_pool("x.pool").seed([("a", "A")])
    wiz = register(Wizard(
        name="x.wiz", title="x",
        steps=[Step(key="src", kind="pick_or_new", prompt="?",
                    pool="x.pool", top_n=3)],
    ))
    runner = ScriptRunner(["a"])
    wiz.run(runner)
    rows = tracelog.get_default().recent(5)
    assert any(r["wizard"] == "x.wiz" and r["status"] == "ok" for r in rows)


def test_cancelled_status():
    wiz = register(Wizard(
        name="c.wiz", title="c",
        steps=[Step(key="ok", kind="confirm", prompt="ok?")],
    ))
    class Boom(ScriptRunner):
        def do_confirm(self, *a, **k): raise Cancelled()
    with pytest.raises(Cancelled):
        wiz.run(Boom([]))
    rows = tracelog.get_default().recent(5)
    assert any(r["wizard"] == "c.wiz" and r["status"] == "cancelled" for r in rows)


def test_error_status_propagates():
    def _boom(ctx): raise RuntimeError("nope")
    wiz = register(Wizard(
        name="e.wiz", title="e",
        steps=[Step(key="x", kind="action", prompt="x", action=_boom)],
    ))
    with pytest.raises(RuntimeError, match="nope"):
        wiz.run(ScriptRunner([]))
    rows = tracelog.get_default().recent(5)
    assert any(r["wizard"] == "e.wiz" and r["status"] == "error" for r in rows)


def test_nested_inherits_parent_trace_id():
    child = register(Wizard(
        name="n.child", title="ch",
        steps=[Step(key="ok", kind="confirm", prompt="?")],
    ))
    parent = register(Wizard(
        name="n.parent", title="pa",
        steps=[Step(key="sub", kind="nested", prompt="sub",
                    nested_wizard="n.child")],
    ))
    parent.run(ScriptRunner([True]))
    rows = tracelog.get_default().recent(10)
    parents = [r for r in rows if r["wizard"] == "n.parent"]
    children = [r for r in rows if r["wizard"] == "n.child"]
    assert parents and children
    assert children[0]["parent_id"] == parents[0]["id"]


def test_nested_ctx_isolation_deepcopy():
    """Child mutating its ctx must NOT bleed back to parent ctx."""
    def _mutate(ctx): ctx["leaked"] = "yes"; return "done"
    child = register(Wizard(
        name="m.child", title="ch",
        steps=[Step(key="m", kind="action", prompt="?", action=_mutate)],
    ))
    parent = register(Wizard(
        name="m.parent", title="pa",
        steps=[Step(key="sub", kind="nested", prompt="sub",
                    nested_wizard="m.child")],
    ))
    result = parent.run(ScriptRunner([]))
    # parent ctx should NOT see the childs leaked key
    assert "leaked" not in result
    # but the child outcome should be stored as parents step value
    assert result["sub"]["m"] == "done"
