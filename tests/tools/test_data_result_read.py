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

def test_reads_a_run_relative_artifact_path(tmp_path, monkeypatch):
    """Analysis artifacts hand the model a path relative to the run dir.

    result.envelope()["full_result_path"] is "results/<artifact_id>.json", with
    no anchor, so the tool has to look for it under the analysis runs directory.
    """
    home = tmp_path / "home"
    run_dir = home / "analysis_runs" / "run_20261113_120000"
    (run_dir / "results").mkdir(parents=True)
    target = run_dir / "results" / "art-123.json"
    target.write_text('{"rows": [1, 2, 3]}', encoding="utf-8")
    monkeypatch.setenv("DACLAW_HOME", str(home))

    payload = json.loads(data_result_read_tool("results/art-123.json"))

    assert payload.get("error") is None
    assert "1, 2, 3" in payload["content"]


def test_relative_path_outside_the_roots_is_still_refused(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DACLAW_HOME", str(home))
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me", encoding="utf-8")

    payload = json.loads(data_result_read_tool("../secret.txt"))
    assert "Access denied" in payload["error"]

def test_oversized_result_is_spilled_to_the_host_when_there_is_no_env(tmp_path, monkeypatch):
    """The data-agent toolset has no terminal, so env is None there.

    Without a sandbox the harness used to truncate inline and lose the output.
    It now spills under DACLAW_HOME, which is exactly where data_result_read
    is allowed to look.
    """
    from tools.tool_result_storage import maybe_persist_tool_result

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DACLAW_HOME", str(home))

    big = "\n".join("line %05d" % i for i in range(6000))
    msg = maybe_persist_tool_result(
        content=big,
        tool_name="data_big_dump",
        tool_use_id="call-1",
        env=None,
        threshold=10_000,
        read_tool="data_result_read",
    )

    assert "<persisted-output>" in msg
    assert "data_result_read" in msg
    assert "read_file" not in msg

    spill = home / "analysis_runs" / "_spill" / "call-1.txt"
    assert spill.is_file()
    assert spill.read_text(encoding="utf-8") == big

    # And the tool can actually read what was written.
    payload = json.loads(data_result_read_tool(str(spill), offset=5900, limit=10))
    assert payload.get("error") is None
    assert "line 05901" in payload["content"]
