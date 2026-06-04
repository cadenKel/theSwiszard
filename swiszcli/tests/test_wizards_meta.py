"""Meta-wizards tests: research + author."""
from __future__ import annotations

import pytest

from swiszcli import pools, wizard_store, wizards_meta
from swiszcli.script_runner import ScriptRunner
from swiszcli.wizard import REGISTRY


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    pools.set_default_db(tmp_path / "pools.db")
    REGISTRY.clear()
    store = wizard_store.WizardStore(tmp_path / "wizards.db")
    wizard_store.set_default(store)
    wizards_meta.register_meta_wizards()
    monkeypatch.setenv("SWISZMEM_URL", "http://127.0.0.1:1")  # force error path
    yield
    REGISTRY.clear()


def test_meta_wizards_registered():
    assert "wizard.research" in REGISTRY
    assert "wizard.author" in REGISTRY


def test_research_runs_and_handles_unreachable_swizmem():
    wiz = REGISTRY["wizard.research"]
    # topic, pick "swizmem.recall" (seeded), action runs, action runs
    out = wiz.run(ScriptRunner(["caden", "swizmem.recall"]))
    assert out["bundle"]["topic"] == "caden"
    assert out["bundle"]["n_snippets"] >= 1
    # error snippet present since the URL is unreachable -> fail-loud-visible
    assert any("error" in s for s in out["bundle"]["snippets"])


def test_author_creates_and_persists_wizard_on_yes():
    wiz = REGISTRY["wizard.author"]
    inputs = [
        "demo.greet", "say hi",
        "name", "text", "what is your name?",
        True,  # commit_now
    ]
    out = wiz.run(ScriptRunner(inputs))
    assert out["result"]["saved"] == "demo.greet"
    assert "demo.greet" in REGISTRY
    # persisted: reload into fresh registry
    REGISTRY.clear()
    n = wizard_store.get_default().load_into_registry()
    assert n >= 1
    assert "demo.greet" in REGISTRY


def test_author_no_commit_leaves_no_wizard():
    wiz = REGISTRY["wizard.author"]
    inputs = [
        "demo.skip", "skip me",
        "name", "text", "?",
        False,  # commit_now
    ]
    out = wiz.run(ScriptRunner(inputs))
    assert "result" not in out  # next-fn ended the walk
    assert "demo.skip" not in REGISTRY


def test_author_unknown_action_ref_fails_loud():
    wiz = REGISTRY["wizard.author"]
    inputs = [
        "demo.bad", "bad",
        "boom", "action", "?",
        True,
    ]
    # default action_ref is empty -> commit raises KeyError
    with pytest.raises(KeyError, match="action_ref"):
        wiz.run(ScriptRunner(inputs))
