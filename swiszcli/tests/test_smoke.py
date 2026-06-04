"""Smoke tests: import everything, exercise protocol + swiszard bridge."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swiszcli.config import Config
from swiszcli.protocol import extract_calls, strip_calls, format_tool_result
from swiszcli.prompt import build_system_prompt, build_memory_block
from swiszcli.swiszard_bridge import load_swiszard_do, SwiszardUnavailable


def test_protocol_extract_simple():
    text = "hi sean\n<<SWISZ>>\nrun BTECHO_X\n<<END>>\nokay"
    calls = extract_calls(text)
    assert len(calls) == 1
    assert calls[0].task == "run BTECHO_X"
    assert strip_calls(text) == "hi sean\n\nokay"


def test_protocol_extract_multi():
    text = "<<SWISZ>>a<<END>> mid <<SWISZ>>b<<END>>"
    calls = extract_calls(text)
    assert [c.task for c in calls] == ["a", "b"]


def test_protocol_case_insensitive_and_whitespace():
    text = "<< swisz >>\n  read /etc/hostname  \n<< end >>"
    calls = extract_calls(text)
    assert len(calls) == 1
    assert calls[0].task == "read /etc/hostname"


def test_protocol_ignores_empty():
    assert extract_calls("<<SWISZ>>   <<END>>") == []


def test_format_tool_result_truncates_loudly():
    big = "x" * 20000
    out = format_tool_result("run x", big)
    assert "truncated" in out
    assert "more chars" in out


def test_prompt_renders():
    sp = build_system_prompt("sess_abc")
    assert "Swiszard" in sp
    assert "<<SWISZ>>" in sp
    assert "sess_abc" in sp


def test_memory_block_empty_and_filled():
    assert build_memory_block([]) == ""
    block = build_memory_block([{"id": 42, "content": "hello world", "trigger_score": 0.91}])
    assert "[mem:42 s=0.91]" in block
    assert "hello world" in block


def test_swiszard_bridge_loads():
    cfg = Config()
    fn = load_swiszard_do(cfg.swiszard_path)
    out = fn("help")
    assert isinstance(out, str) and len(out) > 50


def test_swiszard_bridge_bad_path():
    try:
        load_swiszard_do("/no/such/place")
    except SwiszardUnavailable:
        return
    raise AssertionError("expected SwiszardUnavailable")


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{failed} failed")
    sys.exit(1 if failed else 0)
