"""WizardStore + data-authored wizards tests."""
from __future__ import annotations

import pytest

from swiszcli import callables as wcall
from swiszcli import wizard_store
from swiszcli.script_runner import ScriptRunner
from swiszcli.wizard import REGISTRY, Step, Wizard


@pytest.fixture(autouse=True)
def _clean():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


def test_roundtrip_simple_wizard(tmp_path):
    store = wizard_store.WizardStore(tmp_path / "wizards.db")
    w = Wizard(name="t.simple", title="t",
               steps=[Step(key="ok", kind="confirm", prompt="?")])
    store.save(w, source="llm")
    REGISTRY.clear()
    n = store.load_into_registry()
    assert n == 1
    assert "t.simple" in REGISTRY
    rt = REGISTRY["t.simple"]
    assert rt.title == "t"
    assert rt.steps[0].kind == "confirm"


def test_data_wizard_with_action_ref(tmp_path):
    store = wizard_store.WizardStore(tmp_path / "wizards.db")
    w = Wizard(name="t.act", title="ta",
               steps=[Step(key="x", kind="action", prompt="x",
                           action=wcall.ACTIONS["ctx.dump"])])
    store.save(w)
    REGISTRY.clear()
    store.load_into_registry()
    out = REGISTRY["t.act"].run(ScriptRunner([]))
    # ctx.dump excludes framework keys, so result["x"] should be empty dict
    assert out["x"] == {}


def test_unknown_callable_ref_fails_loud(tmp_path):
    store = wizard_store.WizardStore(tmp_path / "wizards.db")
    # craft a raw row with a bad ref bypassing save()
    import json, time
    store._conn.execute(
        "INSERT INTO wizards VALUES (?, ?, ?, ?, ?)",
        ("t.bad", json.dumps({
            "name": "t.bad", "title": "b",
            "steps": [{"key": "x", "kind": "action", "prompt": "x",
                       "action_ref": "does.not.exist"}]
        }), "llm", time.time(), time.time()),
    )
    store._conn.commit()
    REGISTRY.clear()
    with pytest.raises(KeyError, match="does.not.exist"):
        store.load_into_registry()


def test_unwhitelisted_lambda_save_fails(tmp_path):
    store = wizard_store.WizardStore(tmp_path / "wizards.db")
    w = Wizard(name="t.lam", title="L",
               steps=[Step(key="x", kind="action", prompt="x",
                           action=lambda ctx: 1)])
    with pytest.raises(ValueError, match="whitelist"):
        store.save(w)


def test_code_registry_wins_over_data(tmp_path):
    store = wizard_store.WizardStore(tmp_path / "wizards.db")
    # save a data wizard
    store.save(Wizard(name="t.dup", title="data",
                      steps=[Step(key="ok", kind="confirm", prompt="?")]))
    # pretend code defines one with the same name
    REGISTRY.clear()
    REGISTRY["t.dup"] = Wizard(name="t.dup", title="code",
                               steps=[Step(key="ok", kind="confirm", prompt="?")])
    store.load_into_registry()
    assert REGISTRY["t.dup"].title == "code"


def test_delete(tmp_path):
    store = wizard_store.WizardStore(tmp_path / "wizards.db")
    store.save(Wizard(name="t.del", title="d",
                      steps=[Step(key="ok", kind="confirm", prompt="?")]))
    assert store.delete("t.del") is True
    assert "t.del" not in REGISTRY
    assert store.delete("t.del") is False
