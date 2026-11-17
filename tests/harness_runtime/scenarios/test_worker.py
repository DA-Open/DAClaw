"""The Worker over the full system (app.build_system): P0.6 "--once 跑完一个任务".

Also the lease-loss and cancel races of P0.7 that need a Worker:
a lost lease commits nothing; a cancel during the run ends ``cancelled``
and no tool runs after it.
"""

import asyncio
import json

import pytest

from harness.app import build_system
from harness.config import HarnessConfig
from harness.contracts import (
    Acceptance,
    Actor,
    AgentRole,
    EventType,
    Limits,
    RunLane,
    TaskOrigin,
    TaskRequest,
    TaskStatus,
    TriggerType,
)
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step

pytestmark = pytest.mark.asyncio

ME = Actor(actor_id="u1", scope_id="s1")
MAIN = TaskOrigin(agent_role=AgentRole.MAIN, agent_profile_ref="main@1", run_lane=RunLane.FOREGROUND,
                  trigger_type=TriggerType.USER)
ACCEPTANCE = Acceptance(
    required_artifacts=["draft"],
    checks=[{"id": "artifact_readable"}, {"id": "required_sections", "sections": ["目标", "方案", "计划"]}],
)
DRAFT = "# 草稿\n## 目标\nP0\n## 方案\nharness\n## 计划\n两周\n"


def written_id(ctx, name="draft"):
    msgs = [m for m in ctx.messages if m.get("role") == "tool" and f"wrote artifact {name!r} as " in m.get("content", "")]
    return msgs[-1]["content"].split(" as ")[1].split(" ")[0]


def setup(tmp_path, script, *, heartbeat=15, ttl=60):
    material = tmp_path / "material"
    material.mkdir(exist_ok=True)
    (material / "brief.md").write_text("目标 P0；方案 harness；计划两周。", encoding="utf-8")
    config = HarnessConfig(artifact_root=tmp_path / "artifacts", lease_ttl_sec=ttl, heartbeat_interval_sec=heartbeat)
    spec = {"disabled_tools": [], "users": {"s1": {"main": {"paths": [str(material)]}}}}
    model = ScriptedModelAdapter(script(material))
    system = build_system(config, model, policy_spec=spec)
    return system, model, material


def happy(material):
    return [
        tool_step(("files.read", {"path": str(material / "brief.md")})),
        tool_step(("artifacts.write", {"name": "draft", "content": DRAFT})),
        lambda ctx: result_step("done", {"draft": written_id(ctx)}),
    ]


async def submit(system, **kw):
    req = TaskRequest(request_key=kw.pop("key", "k1"), goal="写项目说明草稿", capability_ref="document_draft@1",
                      acceptance=kw.pop("acceptance", ACCEPTANCE), **kw)
    return await system.tasks.create(req, ME, MAIN)


async def test_run_once_completes_a_task(tmp_path):
    system, model, _ = setup(tmp_path, happy)
    task = await submit(system)
    worker = system.worker(worker_id="w1")
    report = await worker.run_once()
    assert report.committed and report.status is TaskStatus.SUCCEEDED, report
    stored = await system.tasks.get("s1", task.id)
    assert stored.status is TaskStatus.SUCCEEDED and stored.result_ref == report.result_ref
    assert (await system.artifacts.read(stored.result_ref, ME)).content.decode() == DRAFT
    attempt = await system.repos.tasks.get_attempt("s1", report.attempt_id)
    assert attempt.worker_id == "w1" and attempt.ended_at is not None and attempt.exit_reason == "succeeded"
    assert attempt.agent_profile_version == "main@1" and attempt.capability_version == "document_draft@1"
    assert attempt.model_version == "scripted" and attempt.context_ref
    # Nothing left to claim.
    assert await worker.run_once() is None


async def test_role_and_lane_are_recorded(tmp_path):
    system, _, _ = setup(tmp_path, happy)
    task = await submit(system)
    await system.worker().run_once()
    stored = await system.tasks.get("s1", task.id)
    assert stored.agent_role is AgentRole.MAIN and stored.run_lane is RunLane.FOREGROUND
    created = [e for e in await system.repos.events.list("s1", task_id=task.id) if e.type is EventType.TASK_CREATED]
    assert created[0].payload == {"agent_role": "main", "run_lane": "foreground", "status": "queued"}


async def test_worker_only_takes_its_lanes(tmp_path):
    system, _, _ = setup(tmp_path, happy)
    await submit(system)
    background_only = system.worker(lanes=[RunLane.BACKGROUND])
    assert await background_only.run_once() is None


async def test_failed_verification_is_committed_as_failed(tmp_path):
    def missing_section(material):
        return [
            tool_step(("artifacts.write", {"name": "draft", "content": "## 目标\nx\n"})),
            lambda ctx: result_step("done", {"draft": written_id(ctx)}),
        ]

    system, _, _ = setup(tmp_path, missing_section)
    task = await submit(system)
    report = await system.worker().run_once()
    assert report.committed and report.status is TaskStatus.FAILED
    assert "verification_failed" in report.error
    assert (await system.tasks.get("s1", task.id)).status is TaskStatus.FAILED


async def test_over_budget_is_refused_and_committed(tmp_path):
    def never_submits(material):
        return [tool_step(("files.read", {"path": str(material / "brief.md")}))] * 10

    system, _, _ = setup(tmp_path, never_submits)
    await submit(system, limits=Limits(max_model_calls=3))
    report = await system.worker().run_once()
    assert report.committed and report.status is TaskStatus.FAILED
    assert "budget" in report.error


async def test_read_outside_the_whitelist_is_denied_end_to_end(tmp_path):
    secret = tmp_path / "secret.env"
    secret.write_text("KEY=do-not-read", encoding="utf-8")

    def sneaky(material):
        return [
            tool_step(("files.read", {"path": str(secret)})),
            tool_step(("artifacts.write", {"name": "draft", "content": DRAFT})),
            lambda ctx: result_step("done", {"draft": written_id(ctx)}),
        ]

    system, model, _ = setup(tmp_path, sneaky)
    task = await submit(system)
    report = await system.worker().run_once()
    assert report.status is TaskStatus.SUCCEEDED  # it recovered, but never read the file
    assert "do-not-read" not in str([c.model_dump() for c in model.calls])
    actions = await system.repos.actions.list_for_task(task.id)
    assert [a.tool for a in actions] == ["artifacts.write"]


async def test_cancel_during_the_run_ends_cancelled_without_more_tools(tmp_path):
    system, model, material = setup(tmp_path, lambda m: [
        tool_step(("artifacts.write", {"name": "draft", "content": DRAFT})),
        tool_step(("files.read", {"path": str(m / "brief.md")})),
        lambda ctx: result_step("done", {"draft": written_id(ctx)}),
    ])
    task = await submit(system)
    # Cancel as soon as the first tool call has been registered.
    original = system.repos.actions.register
    async def register_then_cancel(action, reserve=None):
        out = await original(action, reserve=reserve)
        if action.tool == "artifacts.write":
            await system.tasks.request_cancel("s1", task.id, "user")
        return out
    system.repos.actions.register = register_then_cancel

    report = await system.worker().run_once()
    assert report.committed and report.status is TaskStatus.CANCELLED, report
    tools = [a.tool for a in await system.repos.actions.list_for_task(task.id)]
    # The write was refused right before running (cancel recheck); nothing ran after.
    assert tools == ["artifacts.write"]
    assert all(str(a.status) != "succeeded" for a in await system.repos.actions.list_for_task(task.id))


@pytest.mark.parametrize("boundary", ["verification", "commit"])
async def test_cancel_at_completion_boundary_finishes_attempt(tmp_path, monkeypatch, boundary):
    system, _, _ = setup(tmp_path, happy)
    task = await submit(system)
    if boundary == "verification":
        verify = system.verifier.verify

        async def cancel_before_verify_returns(*args, **kwargs):
            verified = await verify(*args, **kwargs)
            await system.tasks.request_cancel("s1", task.id, "stop now")
            return verified

        monkeypatch.setattr(system.verifier, "verify", cancel_before_verify_returns)
    else:
        commit = system.tasks.commit

        async def cancel_before_commit(lease, outcome):
            await system.tasks.request_cancel("s1", task.id, "stop now")
            return await commit(lease, outcome)

        monkeypatch.setattr(system.tasks, "commit", cancel_before_commit)

    report = await system.worker().run_once()
    assert report.committed and report.status is TaskStatus.CANCELLED
    assert report.result_ref is None and report.error == "cancelled: stop now"
    stored = await system.tasks.get("s1", task.id)
    assert stored.status is TaskStatus.CANCELLED and stored.current_attempt_id is None
    assert stored.usage.model_calls == 3 and stored.usage.tool_calls == 2
    attempt = await system.repos.tasks.get_attempt("s1", report.attempt_id)
    assert attempt.ended_at is not None and attempt.exit_reason == "cancelled"
    events = await system.repos.events.list("s1", task_id=task.id)
    assert events[-1].payload["to"] == "cancelled"
    assert not any(e.payload.get("to") == "succeeded" for e in events)
    partial = events[-1].payload["artifact_refs"]
    assert partial
    assert (await system.artifacts.read(partial[0], ME)).content.decode() == DRAFT
    assert await system.worker().run_once() is None


async def test_lost_lease_commits_nothing(tmp_path):
    system, model, material = setup(tmp_path, happy, heartbeat=0.01, ttl=60)
    task = await submit(system)
    # Make every heartbeat be refused, as if another worker took over.
    from harness.contracts import StaleLease

    async def refuse(lease):
        raise StaleLease("taken over")
    system.tasks.heartbeat = refuse

    # Slow the first model step so a heartbeat fires before the Runner's next check.
    original_next = model.next_step
    async def slow_next(context, **kw):
        await asyncio.sleep(0.05)
        return await original_next(context, **kw)
    model.next_step = slow_next

    report = await system.worker().run_once()
    assert report.lease_lost and not report.committed
    stored = await system.tasks.get("s1", task.id)
    assert stored.status is TaskStatus.RUNNING  # left to the Scheduler (P1), not committed by a stale worker


async def test_heartbeats_keep_a_long_attempt_alive(tmp_path):
    system, model, _ = setup(tmp_path, happy, heartbeat=0.02, ttl=60)
    original_next = model.next_step
    async def slow_next(context, **kw):
        await asyncio.sleep(0.07)
        return await original_next(context, **kw)
    model.next_step = slow_next
    await submit(system)
    report = await system.worker().run_once()
    assert report.committed and report.status is TaskStatus.SUCCEEDED
    assert report.heartbeats >= 3


async def test_runner_crash_is_committed_as_failed_not_stuck(tmp_path):
    # Review of #18: an exception out of the Runner (a bug) used to escape
    # run_once and leave the task running until the lease ran out.
    system, _, _ = setup(tmp_path, happy)
    task = await submit(system)

    async def crash(*args, **kwargs):
        raise RuntimeError("runner bug")

    system.runner.run = crash
    report = await system.worker().run_once()
    assert report.committed and report.status is TaskStatus.FAILED
    assert "RuntimeError" in report.error and "runner bug" in report.error
    stored = await system.tasks.get("s1", task.id)
    assert stored.status is TaskStatus.FAILED and stored.error.code == "internal"


async def test_unknown_profile_is_committed_as_failed_not_stuck(tmp_path):
    system, _, _ = setup(tmp_path, happy)
    task = await submit(system)
    # Simulate a profile removed after the task was created.
    system.agents._profiles.clear()
    report = await system.worker().run_once()
    assert report.committed and report.status is TaskStatus.FAILED and "main@1" in report.error
    assert (await system.tasks.get("s1", task.id)).status is TaskStatus.FAILED


async def test_cli_runs_one_task(tmp_path, monkeypatch, capsys):
    from harness import worker as cli

    material = tmp_path / "material"
    material.mkdir()
    (material / "brief.md").write_text("材料", encoding="utf-8")
    (tmp_path / "policy.yaml").write_text(f"users:\n  s1:\n    main:\n      paths: ['{material.as_posix()}']\n", encoding="utf-8")
    (tmp_path / "task.json").write_text(json.dumps({
        "request_key": "cli-1", "scope_id": "s1", "actor_id": "u1",
        "goal": "写草稿", "capability_ref": "document_draft@1",
        "acceptance": ACCEPTANCE.model_dump(mode="json"),
        "input_refs": [f"file:{(material / 'brief.md').as_posix()}"],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "script.json").write_text(json.dumps([
        {"tool": "files.read", "args": {"path": (material / "brief.md").as_posix()}},
        {"tool": "artifacts.write", "args": {"name": "draft", "content": DRAFT}},
        {"submit": {"summary": "done", "artifacts": {"draft": "@draft"}}},
    ], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("DARAOS_HARNESS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DARAOS_HARNESS_POLICY_FILE", str(tmp_path / "policy.yaml"))
    code = await asyncio.to_thread(
        cli.main,
        ["--once", "--submit", str(tmp_path / "task.json"), "--scripted", str(tmp_path / "script.json"), "--print-result"],
    )
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert code == 0, lines
    assert lines[0]["status"] == "queued"
    assert lines[1]["status"] == "succeeded" and lines[1]["committed"] and lines[1]["result"] == DRAFT
