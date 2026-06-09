"""
Regression tests for the 4-phase harness turn loop (PM #512-514).
Guards against re-introducing the swisz_efd4ced6 failure mode.
"""
import pytest
from swiszcli import cli


def test_situation_template_in_system_prompt():
    """SITUATION block must be present in SYSTEM_PROMPT — it is the glass retrieval key."""
    assert "SITUATION" in cli.SYSTEM_PROMPT


def test_situation_template_has_all_fields():
    for field in ("project", "user_intent", "model_intent",
                  "recent_tools", "pm_nodes", "working_file"):
        assert field in cli.SITUATION_TEMPLATE, f"SITUATION_TEMPLATE missing field: {field}"


def test_harness_orient_returns_string(monkeypatch):
    monkeypatch.setattr(cli, "context_recall", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "mem_recall_triggers", lambda *a, **kw: [])
    result = cli._harness_orient(
        session_id="test_sess", turn=1,
        user_msg="sup homie", pm_orient_cache="[pm_orient stub]",
    )
    assert isinstance(result, str)
    assert "[pm_orient]" in result


def test_deliberate_non_fatal_on_model_failure(monkeypatch):
    class _FakeCfg:
        model = "test-model"
        provider = "ollama"
        provider_base_url = "http://127.0.0.1:19999"
        provider_api_key = ""

    monkeypatch.setattr(cli, "glass_consult", lambda *a, **kw: "")
    filled, glass = cli._deliberate("sup homie", "[ctx]", [], _FakeCfg())
    assert filled == "" and glass == ""


def test_deliberate_two_pass_reconsider(monkeypatch):
    """
    Core regression for PM #514:
    - glass must be called twice on genuinely different inputs (S1 then S2)
    - model gets a reconsider prompt between passes (not glass called twice on same input)
    - raw user message must never be the glass input
    """
    call_seq = []  # records ("llm"|"glass", input_snippet)

    fake_situation_base = "<SITUATION>\nproject: swiszard\nactive_objective: test\nlast_user_intent: greeting\nrecent_tools: none\nactive_pm_nodes: none\nworking_file: none\n</SITUATION>"

    llm_call_count = [0]

    def _fake_llm_fill(prompt, cfg):
        llm_call_count[0] += 1
        # first call: situation fill; subsequent calls: reconsider (may return slightly modified)
        call_seq.append(("llm", prompt[:80]))
        return fake_situation_base.replace("greeting", f"greeting_v{llm_call_count[0]}")

    glass_inputs = []

    def _fake_glass(thought, **kw):
        glass_inputs.append(thought)
        call_seq.append(("glass", thought[:80]))
        return f"warning: last time you did this, something bad happened (pass {len(glass_inputs)})"

    class _FakeCfg:
        model = "test-model"
        provider = "ollama"
        provider_base_url = "http://127.0.0.1:19999"
        provider_api_key = ""

    monkeypatch.setattr(cli, "_llm_fill", _fake_llm_fill)
    monkeypatch.setattr(cli, "glass_consult", _fake_glass)

    final_situation, glass_text = cli._deliberate("sup homie", "[ctx]", [], _FakeCfg())

    # glass called exactly twice
    assert len(glass_inputs) == 2, f"expected 2 glass calls, got {len(glass_inputs)}"

    # glass inputs are different (S1 != S2 because model updated intent)
    assert glass_inputs[0] != glass_inputs[1], "glass called twice on identical input — not two genuine passes"

    # raw user message never went to glass
    for g_input in glass_inputs:
        assert "sup homie" not in g_input, "glass received raw user message instead of filled SITUATION"

    # final situation is S3 (third llm call output)
    assert llm_call_count[0] == 3, f"expected 3 model calls (fill + reconsider x2), got {llm_call_count[0]}"

    # both glass warnings present in glass_text
    assert "pass 1" in glass_text and "pass 2" in glass_text
