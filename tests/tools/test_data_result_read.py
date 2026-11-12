"""data_result_read: path containment, pagination, and the hint rewrite."""

from __future__ import annotations

import json

import pytest

from tools.data_result_read_tool import MAX_LIMIT, data_result_read_tool
from tools.tool_result_storage import _build_persisted_message
from toolsets import TOOLSETS


@pytest.fixture()
def results_dir(tmp_path, monkeypatch):
    root = tmp_path / "daclaw-results"
    root.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return root


def test_reads_a_file_under_the_results_dir(results_dir):
    target = results_dir / "result-1.txt"
    target.write_text("\n".join("line %d" % i for i in range(1, 11)), encoding="utf-8")

    payload = json.loads(data_result_read_tool(str(target)))

    assert payload.get("error") is None
    assert "line 1" in payload["content"]
    assert payload.get("total_lines") is not None


def test_refuses_paths_outside_the_allowed_roots(results_dir):
    outside = results_dir.parent / "secret.txt"
    outside.write_text("do not read me", encoding="utf-8")

    payload = json.loads(data_result_read_tool(str(outside)))
    assert "Access denied" in payload["error"]


def test_refuses_traversal_out_of_the_results_dir(results_dir):
    outside = results_dir.parent / "secret.txt"
    outside.write_text("do not read me", encoding="utf-8")
    sneaky = str(results_dir / ".." / "secret.txt")

    payload = json.loads(data_result_read_tool(sneaky))
    assert "Access denied" in payload["error"]


def test_refuses_system_files(results_dir):
    payload = json.loads(data_result_read_tool("/etc/passwd"))
    assert "Access denied" in payload["error"]


def test_reports_missing_file(results_dir):
    payload = json.loads(data_result_read_tool(str(results_dir / "nope.txt")))
    assert "No such result file" in payload["error"]


def test_limit_is_capped(results_dir):
    target = results_dir / "big.txt"
    target.write_text("\n".join("l%d" % i for i in range(3000)), encoding="utf-8")

    payload = json.loads(data_result_read_tool(str(target), offset=1, limit=999_999))
    assert payload.get("error") is None
    # 2000 lines max, plus at most a couple of header/footer lines
    assert len(payload["content"].splitlines()) <= MAX_LIMIT + 2


def test_hint_names_the_tool_that_actually_exists():
    with_data_tool = _build_persisted_message(
        "preview", True, 1_000_000, "/tmp/daclaw-results/x.txt", read_tool="data_result_read"
    )
    assert "data_result_read" in with_data_tool
    assert "read_file" not in with_data_tool

    default_hint = _build_persisted_message("preview", True, 1_000_000, "/x.txt")
    assert "read_file" in default_hint


def test_agent_picks_the_tool_present_in_its_toolset():
    from run_agent import AIAgent

    class _Stub:
        def __init__(self, names):
            self.valid_tool_names = names

    assert AIAgent._persisted_read_tool(_Stub({"read_file", "data_result_read"})) == "read_file"
    assert AIAgent._persisted_read_tool(_Stub({"data_query", "data_result_read"})) == "data_result_read"
    assert AIAgent._persisted_read_tool(_Stub(set())) == "read_file"


def test_data_agent_chat_toolset_exposes_the_tool():
    assert "data_result_read" in TOOLSETS["data-agent-chat"]["tools"]
