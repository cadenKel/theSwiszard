"""Tool-result archive + truncation pointer tests."""
from __future__ import annotations

import pytest
from swiszcli import archive as toolarchive
from swiszcli.archive import ToolArchive
from swiszcli.protocol import format_tool_result


def test_archive_roundtrip(tmp_path):
    arch = ToolArchive(tmp_path, "swisz_abc")
    ref = arch.write("read /etc/hostname", "hello\nworld\n")
    assert ref.startswith("[archive:swisz_abc/")
    body = arch.read(ref)
    assert body == "hello\nworld\n"
    meta = arch.meta(ref)
    assert meta["task"] == "read /etc/hostname"
    assert meta["len"] == len("hello\nworld\n")


def test_seq_monotonic(tmp_path):
    arch = ToolArchive(tmp_path, "s1")
    r1 = arch.write("a", "x")
    r2 = arch.write("b", "y")
    assert r1 != r2
    assert int(r1.split("/")[-1].rstrip("]")) + 1 == int(r2.split("/")[-1].rstrip("]"))


def test_format_truncation_includes_ref():
    big = "x" * 10000
    out = format_tool_result("t", big, archive_ref="[archive:s/000001]", max_chars=100)
    assert "truncated" in out
    assert "[archive:s/000001]" in out
    assert len(out) < 500


def test_format_no_truncation_no_ref_marker():
    out = format_tool_result("t", "hi", archive_ref="[archive:s/000001]", max_chars=100)
    assert "truncated" not in out
    assert "[archive:" not in out  # ref only appears when truncated


def test_bad_ref_raises(tmp_path):
    arch = ToolArchive(tmp_path, "sx")
    with pytest.raises(ValueError):
        arch.read("not-a-ref")
    with pytest.raises(FileNotFoundError):
        arch.read("[archive:sx/999999]")


def test_default_singleton(tmp_path):
    arch = ToolArchive(tmp_path, "sd")
    toolarchive.set_default(arch)
    assert toolarchive.get_default() is arch