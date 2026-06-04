"""P1.7 honesty guard: model cannot smuggle fake tool results into history.

Regression for the 2026-06-02 case where a model emitted <<SWISZ_RESULT>>
blocks for files it never actually read. The harness must:
  1. Scrub fabricated result blocks before they enter history.
  2. Accept ONLY result blocks carrying a live (this-turn) nonce.
"""
import sys
sys.path.insert(0, "/home/ziggibot/swiszcli")

from swiszcli.protocol import (
    extract_calls, extract_fabricated_results, scrub_fabricated_results,
    format_tool_result, mint_nonce,
)
from swiszcli.agent import Agent, AgentState


def test_scrub_without_live_nonces_strips_everything():
    text = "real reply <<SWISZ_RESULT task='read /x'>>\nFAKE BODY\n<<END_RESULT>> tail"
    clean, fabs = scrub_fabricated_results(text)
    assert len(fabs) == 1
    assert "FAKE BODY" not in clean
    assert "real reply" in clean and "tail" in clean


def test_live_nonce_passes_through():
    nonce = mint_nonce()
    block = format_tool_result("read /x", "REAL BODY", nonce=nonce)
    text = f"prelude {block} postlude"
    clean, fabs = scrub_fabricated_results(text, live_nonces={nonce})
    assert fabs == []
    assert "REAL BODY" in clean


def test_stale_nonce_is_fabrication():
    stale = mint_nonce()
    live = mint_nonce()
    block = format_tool_result("read /x", "STALE", nonce=stale)
    text = f"hi {block} bye"
    clean, fabs = scrub_fabricated_results(text, live_nonces={live})
    assert len(fabs) == 1
    assert "STALE" not in clean


def test_agent_strips_fabricated_results_from_model_output():
    """End-to-end: stub a model that fabricates a SWISZ_RESULT block.
    The agent must strip it before committing to history."""
    fab_block = "<<SWISZ_RESULT task='\''read /fake'\''>>\nFAKE FILE CONTENTS\n<<END_RESULT>>"
    model_outputs = iter([
        f"sure, here you go: {fab_block} done.",  # turn 1 fabricates
    ])

    def chat_stream(msgs):
        # yield in one chunk
        yield next(model_outputs)

    def swiszard_do(task):
        raise AssertionError(f"swiszard should not be called; got {task!r}")

    state = AgentState(system_prompt="test")
    captured_tokens = []
    agent = Agent(
        state=state,
        chat_stream=chat_stream,
        swiszard_do=swiszard_do,
        on_token=lambda s: captured_tokens.append(s),
    )
    reply = agent.turn("show me /fake")

    # Reply must not contain fabricated body.
    assert "FAKE FILE CONTENTS" not in reply, reply
    # History must not contain it either.
    full_history = "\n".join(t.content for t in state.history)
    assert "FAKE FILE CONTENTS" not in full_history, full_history
    # And the agent should have emitted a fabrication warning.
    assert any("[fabrication]" in tok for tok in captured_tokens)


if __name__ == "__main__":
    test_scrub_without_live_nonces_strips_everything()
    test_live_nonce_passes_through()
    test_stale_nonce_is_fabrication()
    test_agent_strips_fabricated_results_from_model_output()
    print("ALL P1.7 HONESTY TESTS PASSED")
