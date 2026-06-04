"""Phase 7: SOUL/catalog prompt, session log, ctx budget."""
from __future__ import annotations

import pytest

from swiszcli import ctx_budget, prompt, sessions, wizard_store
from swiszcli.wizard import REGISTRY, Step, Wizard


def test_ctx_budget_soft_hard():
    v = ctx_budget.check("x" * (32_000 * 4 + 8))
    assert v.soft and not v.hard
    v2 = ctx_budget.check("x" * (64_000 * 4 + 8))
    assert v2.hard
    with pytest.raises(ctx_budget.ContextOverflow):
        ctx_budget.enforce("x" * (64_000 * 4 + 8))


def test_soul_injection(tmp_path, monkeypatch):
    soul = tmp_path / "SOUL.md"
    soul.write_text("LOUD-VOICE-MARKER")
    monkeypatch.setenv("SWISZCLI_SOUL", str(soul))
    out = prompt.build_system_prompt_full("sess123")
    assert "LOUD-VOICE-MARKER" in out
    assert "<soul>" in out


def test_wizard_catalog_injection(monkeypatch, tmp_path):
    monkeypatch.setenv("SWISZCLI_SOUL", str(tmp_path / "missing.md"))
    REGISTRY.clear()
    REGISTRY["t.cat"] = Wizard(name="t.cat", title="cat test",
                               steps=[Step(key="ok", kind="confirm", prompt="?")])
    try:
        out = prompt.build_system_prompt_full("s1")
        assert "<wizard-catalog>" in out
        assert "t.cat" in out
    finally:
        REGISTRY.clear()


def test_session_log_fts_or_like(tmp_path):
    sl = sessions.SessionLog(tmp_path / "sessions.db")
    sl.log("s1", "user", "hello world")
    sl.log("s1", "assistant", "good morning sean")
    sl.log("s2", "user", "different session")
    assert len(sl.by_session("s1")) == 2
    hits = sl.search("morning")
    assert any("morning" in h["content"] for h in hits)
    sl.close()
