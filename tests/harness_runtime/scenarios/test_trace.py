"""trace.py: one task's timeline, read from the records the harness wrote."""

import asyncio
import json

import pytest

from harness.contracts import Limits
from harness.runtime.models import result_step, tool_step
from harness.trace import build_trace, render_trace

from .test_worker import ACCEPTANCE, DRAFT, happy, setup, submit, written_id

pytestmark = pytest.mark.asyncio


async def run(tmp_path, script, **kw):
    system, model, material = setup(tmp_path, script)
    task = await submit(system, **kw)
    report = await system.worker().run_once()
    trace = await build_trace(system.repos, system.artifacts, "s1", task.id)
    return system, model, trace, report


async def test_timeline_of_a_successful_task(tmp_path):
    system, model, trace, _ = await run(tmp_path, happy)
    kinds = [s["type"] for s in trace["steps"]]
    assert kinds == [
        "task_created",
        "task_status_changed",
        "context_built", "model_call", "tool_action",
        "context_built", "model_call", "tool_action",
        "context_built", "model_call",
        "verification_recorded",
        "task_status_changed",
    ]
    assert [s["tool"] for s in trace["steps"] if s["type"] == "tool_action"] == ["files.read", "artifacts.write"]
    assert "tool_refused" not in kinds  # registered calls are tool_action only, never duplicated
    assert trace["steps"][-1]["to"] == "succeeded"
    assert trace["task"]["status"] == "succeeded" and trace["task"]["result_ref"]
    assert trace["usage"]["model_calls"] == 3 and trace["usage"]["tool_calls"] == 2
    assert len(trace["attempts"]) == 1 and trace["attempts"][0]["exit_reason"] == "succeeded"
    # Every model call's input is an artifact the reader can open.
    ref = next(s["input_ref"] for s in trace["steps"] if s["type"] == "model_call")
    assert ref.startswith("art_")
    text = render_trace(trace)
    assert "[succeeded]" in text and "verify             passed" in text
    assert "model calls 3/20" in text
    # Pure JSON, so --trace FILE.json can write it.
    json.dumps(trace)


async def test_refused_calls_show_without_their_content(tmp_path):
    secret = tmp_path / "secret.env"
    secret.write_text("KEY=do-not-read", encoding="utf-8")

    def sneaky(material):
        return [
            tool_step(("files.read", {"path": str(secret)})),
            tool_step(("shell.run", {"cmd": "cat /etc/passwd"})),
            tool_step(("artifacts.write", {"name": "draft", "content": DRAFT})),
            lambda ctx: result_step("done", {"draft": written_id(ctx)}),
        ]

    _, _, trace, _ = await run(tmp_path, sneaky)
    refused = [s for s in trace["steps"] if s["type"] == "tool_refused"]
    assert [(r["tool"], r["status"]) for r in refused] == [("files.read", "denied"), ("shell.run", "rejected")]
    assert "outside the directories" in refused[0]["error"]
    assert "unknown tool" in refused[1]["error"]
    # Shown right after the model call that asked for it.
    i = trace["steps"].index(refused[0])
    assert trace["steps"][i - 1]["type"] == "model_call"
    assert "do-not-read" not in json.dumps(trace)
    assert "REFUSED denied" in render_trace(trace)


async def test_failure_shows_the_error_and_the_wrap_up_turn(tmp_path):
    def never_submits(material):
        return [tool_step(("files.read", {"path": str(material / "brief.md")}))] * 10

    _, _, trace, _ = await run(tmp_path, never_submits, limits=Limits(max_model_calls=3))
    assert trace["task"]["status"] == "failed"
    assert "budget" in trace["task"]["error"]["message"]
    contexts = [s for s in trace["steps"] if s["type"] == "context_built"]
    assert contexts[-1]["wrap_up"] is True and not any(c["wrap_up"] for c in contexts[:-1])
    reminders = [s for s in trace["steps"] if s["type"] == "budget_reminder"]
    assert reminders, "crossing a tier writes a reminder"
    text = render_trace(trace)
    assert "[wrap-up: only submit_result offered]" in text and "error " in text


async def test_cli_trace_writes_json(tmp_path, monkeypatch, capsys):
    from harness import worker as cli

    material = tmp_path / "material"
    material.mkdir()
    (material / "brief.md").write_text("材料", encoding="utf-8")
    (tmp_path / "policy.yaml").write_text(f"users:\n  s1:\n    main:\n      paths: ['{material.as_posix()}']\n", encoding="utf-8")
    (tmp_path / "task.json").write_text(json.dumps({
        "request_key": "cli-t", "scope_id": "s1", "actor_id": "u1", "goal": "写草稿",
        "capability_ref": "document_draft@1", "acceptance": ACCEPTANCE.model_dump(mode="json"),
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "script.json").write_text(json.dumps([
        {"tool": "artifacts.write", "args": {"name": "draft", "content": DRAFT}},
        {"submit": {"summary": "done", "artifacts": {"draft": "@draft"}}},
    ], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("DARAOS_HARNESS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DARAOS_HARNESS_POLICY_FILE", str(tmp_path / "policy.yaml"))
    out = tmp_path / "trace.json"
    base = ["--once", "--submit", str(tmp_path / "task.json"), "--scripted", str(tmp_path / "script.json")]
    assert await asyncio.to_thread(cli.main, [*base, "--trace", str(out)]) == 0
    trace = json.loads(out.read_text(encoding="utf-8"))
    assert trace["task"]["status"] == "succeeded" and trace["steps"][-1]["to"] == "succeeded"
    capsys.readouterr()
    # Bare --trace prints the text timeline to stderr; stdout stays one JSON line per attempt.
    assert await asyncio.to_thread(cli.main, [*base, "--trace"]) == 0
    captured = capsys.readouterr()
    assert "verify             passed" in captured.err
    assert all(json.loads(l) for l in captured.out.splitlines() if l.strip())
