"""Tests for pick_or_new + ChoicePool, driven by ScriptRunner."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from swiszcli import pools
from swiszcli.script_runner import ScriptRunner
from swiszcli.wizard import REGISTRY, Step, Wizard


@pytest.fixture(autouse=True)
def _tmp_pools_db(monkeypatch, tmp_path):
    db = tmp_path / "pools.db"
    pools.set_default_db(db)
    # Clear REGISTRY between tests so register() does not collide
    REGISTRY.clear()
    yield db


def _build_wiz(name: str = "t.pickornew") -> Wizard:
    return Wizard(
        name=name, title="t",
        steps=[Step(key="src", kind="pick_or_new", prompt="pick a source",
                    pool="t.research.sources", top_n=5,
                    new_prompt="type new source")],
    )


def test_seed_then_pick_existing():
    p = pools.get_pool("t.research.sources")
    p.seed([("swizmem.recall", "swizmem"), ("file.grep", "grep")])
    wiz = _build_wiz()
    runner = ScriptRunner(["swizmem.recall"])
    out = wiz.run(runner)
    assert out["src"] == "swizmem.recall"
    e = p.find("swizmem.recall")
    assert e is not None and e.use_count == 1


def test_pick_or_new_appends_new_value():
    p = pools.get_pool("t.research.sources")
    p.seed([("file.grep", "grep")])
    wiz = _build_wiz()
    runner = ScriptRunner(["arxiv.search"])
    out = wiz.run(runner)
    assert out["src"] == "arxiv.search"
    e = p.find("arxiv.search")
    assert e is not None
    assert e.created_by == "user"
    assert e.use_count == 1


def test_ranking_by_usage():
    p = pools.get_pool("t.research.sources")
    p.seed([("a", "A"), ("b", "B"), ("c", "C")])
    # Drive a to top via touches
    for _ in range(5):
        p.touch("a")
    top = p.top(3)
    assert top[0].value == "a"


def test_script_exhausted_raises():
    pools.get_pool("t.research.sources").seed([("x", "X")])
    wiz = _build_wiz()
    runner = ScriptRunner([])  # empty
    with pytest.raises(RuntimeError, match="script exhausted"):
        wiz.run(runner)
