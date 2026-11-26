"""R4 exercises real HTTP over loopback, approval, durable replay, and verification."""

import json
import logging
import os
import re
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event as ThreadEvent, Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml

from harness.app import PolicyFileError, build_system, load_policy_file
from harness.config import HarnessConfig
from harness.contracts import (
    Acceptance, Action, ActionRef, ActionStatus, ApprovalDecision, ArtifactInput, CandidateResult,
    ErrorCode, EventType, ExecutionContext, InvalidRequest, NotFound,
    Task, TaskOutcome, TaskRequest, TaskStatus, ToolCall, ToolStatus, Usage, VerificationStatus, WaitCondition,
)
from harness.personal.baseline import BaselineWriter
from harness.runtime.messages import SUBMIT_RESULT, ToolCallPart
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.runtime.models.base import build_step
from harness.tasks.approvals import Approvals
from harness.tasks.recovery import needs_manual_confirmation
from harness.tools import connectors
from harness.trace import build_trace
from tests.harness_runtime.fakes import ACTOR, Clock
from tests.harness_runtime.scenarios.test_api import Api, assert_error
from tests.harness_runtime.scenarios.test_worker import MAIN, written_id

TOOL = "feishu.send_message"
CHECKS = ("artifact_readable", "feishu_receipt")
TARGET = "chat_id:fixture-room"
ARGS = {"receive_id_type": "chat_id", "receive_id": "fixture-room", "text": "离线验收\n第二行"}
CALL = ToolCall(tool=TOOL, args=ARGS, logical_key="step-1.0")
AUTH = "/open-apis/auth/v3/tenant_access_token/internal"
SEND = "/open-apis/im/v1/messages"
WINDOW = "outside Feishu 55-minute replay window (1 hour minus a 5-minute margin)"


@pytest.fixture
def feishu(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("FEISHU_TEST_"):
            monkeypatch.delenv(name)
    state = SimpleNamespace(
        app_id=uuid4().hex, secret=uuid4().hex, token=uuid4().hex, calls=[], messages={},
        mode="success", auth_reply=None, auth_hook=lambda: None, delayed=ThreadEvent(),
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Keep request bodies and credentials out of the test log.

        def reply(self, http, body):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(http)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            url = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if url.path == AUTH:
                state.calls.append(("auth",))
                if body != {"app_id": state.app_id, "app_secret": state.secret}:
                    return self.reply(403, {"code": 1, "msg": state.secret})
                state.auth_hook()
                if state.mode in {"auth_disconnect", "auth_timeout"}:
                    if state.mode == "auth_timeout":
                        state.delayed.wait(5)
                    self.close_connection = True
                    return
                if state.auth_reply is not None:
                    return self.reply(*state.auth_reply)
                return self.reply(200, {"code": 0, "tenant_access_token": state.token})
            assert url.path == SEND
            assert self.headers["Authorization"] == "Bearer " + state.token
            state.calls.append(("send", parse_qs(url.query), body))
            if state.mode == "disconnect":
                self.close_connection = True
                return
            errors = {
                "forbidden": (403, {"code": 230001, "msg": state.secret}),
                "business": (200, {"code": 230001, "msg": state.token}),
                "bot_removed": (400, {"code": 230002}),
                "busy": (503, {"code": 1, "msg": state.token}),
                "throttled": (429, {"code": 1}),
                "group_throttle": (400, {"code": 230020, "data": {}}),
                "api_throttle": (200, {"code": 99991400, "data": {}}),
                "in_flight": (400, {"code": 230049, "data": {}}),
                "invalid_target": (200, {"code": 230034, "data": {}}),
                "unrecognized": (200, {"code": 7777}),
                "missing_data": (200, {"code": 0}),
                "invalid_code": (200, {"code": False, "data": {"message_id": "bogus"}}),
                "bad_http": (403, b"invalid-json"),
                "redirect": (307, {}),
                "malformed": (200, b"not-json"),
                "no_receipt": (200, {"code": 0, "data": {"message_id": ""}}),
            }
            if state.mode in errors:
                return self.reply(*errors[state.mode])
            receipt = state.messages.setdefault(body["uuid"], "fixture-message-" + uuid4().hex)
            if state.mode == "accept_timeout":
                state.delayed.wait(5)  # The client times out after acceptance; discard this response.
                return
            self.reply(200, {"code": 0, "msg": "success", "data": {"message_id": receipt}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("R4_FIXTURE_APP_ID", state.app_id)
    monkeypatch.setenv("R4_FIXTURE_APP_SECRET", state.secret)
    try:
        yield state
    finally:
        state.delayed.set()
        server.shutdown()
        server.server_close()
        thread.join()


def setup(tmp_path, feishu, *, deployment=(TARGET,), grant=None, script=()):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(yaml.safe_dump({"users": {"s1": {"main": grant or {}}}}), encoding="utf-8")
    config = replace(
        HarnessConfig(), artifact_root=tmp_path / "artifacts", policy_file=policy_path,
        feishu_base_url=feishu.url, feishu_app_id_env="R4_FIXTURE_APP_ID",
        feishu_app_secret_env="R4_FIXTURE_APP_SECRET", feishu_allowed_recipients=deployment,
        feishu_timeout_sec=0.25, reconcile_grace_sec=1,
    )
    model = ScriptedModelAdapter(script)
    return build_system(config, model, clock=Clock()), model


def task_request(key, checks):
    return TaskRequest(
        request_key=key, goal="把验收消息发给测试收件人", capability_ref="feishu_message@1",
        acceptance=Acceptance(required_artifacts=["receipt"], checks=[{"id": name} for name in checks]),
    )


async def create(system, *, key="r4", checks=CHECKS):
    return await system.tasks.create(task_request(key, checks), ACTOR, MAIN)


async def claim(system):
    lease = await system.tasks.claim("worker-r4", [MAIN.run_lane])
    assert lease
    return ExecutionContext(lease=lease, actor=ACTOR, agent_role=MAIN.agent_role, run_lane=MAIN.run_lane)


async def park(system, execution, condition):
    await system.tasks.commit(execution.lease, TaskOutcome(status=TaskStatus.WAITING, wait_condition=condition))


async def approve(system, task):
    action, = await system.repos.actions.list_for_task(task.id)
    await Approvals(system.repos, clock=system.clock).decide(
        ACTOR.scope_id, task.id, action.id, actor=ACTOR, decision=ApprovalDecision.APPROVE,
        request_hash=action.request_hash, expires_in_sec=86400,
    )
    return action


async def approved(system, key="r4", *, checks=CHECKS):
    task = await create(system, key=key, checks=checks)
    before = await claim(system)
    first = await system.gateway.invoke(before, CALL)
    assert first.status is ToolStatus.NEEDS_APPROVAL
    await park(system, before, WaitCondition.APPROVAL)
    action = await approve(system, task)
    return task, action, await claim(system)


async def timeout(system, feishu, key="r4"):
    task, action, execution = await approved(system, key)
    feishu.mode = "accept_timeout"
    outcome = await system.gateway.invoke(execution, CALL)
    assert outcome.status is ToolStatus.UNKNOWN and outcome.receipt_ref is None
    assert action.id in feishu.messages
    await park(system, execution, WaitCondition.RECONCILIATION)
    feishu.mode = "success"
    return task, action


@pytest.mark.asyncio
@pytest.mark.parametrize("checks", [(), ("artifact_readable",)])
async def test_feishu_create_requires_receipt_check(tmp_path, feishu, checks):
    system, _ = setup(tmp_path, feishu)
    with pytest.raises(InvalidRequest, match="feishu_receipt"):
        await create(system, checks=checks)
    assert feishu.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("checks", [(), ("artifact_readable",)])
async def test_feishu_http_requires_receipt_check(tmp_path, feishu, checks):
    api = Api(tmp_path, script=lambda _: [], feishu_base_url=feishu.url)
    request = task_request("r4", checks)
    async with api.client:
        response = await api.submit(key=request.request_key, capability_ref=request.capability_ref,
                                    input_refs=[], acceptance=request.acceptance.model_dump(mode="json"))
        assert_error(response, 400, "invalid_request")
        assert "feishu_receipt" in response.json()["message"]
        accepted = await api.submit(key=request.request_key, capability_ref=request.capability_ref,
                                    input_refs=[], acceptance={"checks": [{"id": "feishu_receipt"}]})
        assert accepted.status_code == 202
    assert feishu.calls == []


@pytest.mark.asyncio
async def test_feishu_send_requires_approval(tmp_path, feishu):
    system, _ = setup(tmp_path, feishu)
    task = await create(system)
    result = await system.gateway.invoke(await claim(system), CALL)
    action, = await system.repos.actions.list_for_task(task.id)
    assert result.status is ToolStatus.NEEDS_APPROVAL and action.status is ActionStatus.NEEDS_APPROVAL
    assert result.action_id == action.id and feishu.calls == []
    spec, = (s for s in system.gateway.specs if s.name == TOOL)
    assert not spec.read_only and spec.side_effects and spec.idempotent and spec.supports_reconcile


@pytest.mark.asyncio
async def test_feishu_send_approved_once(tmp_path, feishu):
    system, _ = setup(tmp_path, feishu, grant={"recipients": [TARGET]})
    _, action, execution = await approved(system)
    result = await system.gateway.invoke(execution, CALL)
    replay = await system.gateway.invoke(execution, CALL)
    assert result.status is replay.status is ToolStatus.SUCCEEDED
    assert result.receipt_ref == replay.receipt_ref == feishu.messages[action.id]
    assert re.fullmatch(r"act_[0-9a-f]{32}", action.id)
    assert [call[0] for call in feishu.calls] == ["auth", "send"]
    _, query, body = feishu.calls[1]
    assert query == {"receive_id_type": ["chat_id"]}
    assert body == {"receive_id": ARGS["receive_id"], "msg_type": "text", "content": body["content"], "uuid": action.id}
    assert json.loads(body["content"]) == {"text": ARGS["text"]}


@pytest.mark.asyncio
async def test_feishu_send_timeout_reconcile_same_uuid(tmp_path, feishu):
    system, _ = setup(tmp_path, feishu)
    task, action = await timeout(system, feishu)
    system.clock.advance(3299)
    await system.scheduler.recovery.tick(system.clock())
    settled, = await system.repos.actions.list_for_task(task.id)
    assert settled.status is ActionStatus.SUCCEEDED and settled.receipt_ref == feishu.messages[action.id]
    assert feishu.calls[1] == feishu.calls[3] and feishu.calls[1][2]["uuid"] == action.id
    assert len(feishu.messages) == 1 and len(feishu.calls) == 4
    assert (await system.tasks.get(ACTOR.scope_id, task.id)).status is TaskStatus.QUEUED


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [3300, 3600, 3601])
async def test_feishu_reconcile_after_one_hour_unsupported(tmp_path, feishu, age):
    system, _ = setup(tmp_path, feishu)
    task, _ = await timeout(system, feishu)
    system.clock.advance(age)
    await system.scheduler.recovery.tick(system.clock())
    await system.scheduler.recovery.tick(system.clock())
    events = await system.repos.events.list(ACTOR.scope_id, task_id=task.id)
    history = [e for e in events if e.type is EventType.TOOL_RECONCILE]
    assert [e.payload["status"] for e in history] == ["unsupported"] and needs_manual_confirmation(history)
    assert (await system.tasks.get(ACTOR.scope_id, task.id)).wait_condition is WaitCondition.RECONCILIATION
    assert len(feishu.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("deployment", "grant"), [
    ((), {"recipients": [TARGET]}), (("open_id:fixture-room",), {}),
    ((TARGET,), {"recipients": ["chat_id:other"]}), ((TARGET,), {"recipients": []}),
])
async def test_feishu_recipient_outside_whitelist_denied(tmp_path, feishu, deployment, grant):
    system, _ = setup(tmp_path, feishu, deployment=deployment, grant=grant)
    task = await create(system)
    result = await system.gateway.invoke(await claim(system), CALL)
    assert result.status is ToolStatus.DENIED and result.error.code is ErrorCode.FORBIDDEN
    assert result.action_id is None and await system.repos.actions.list_for_task(task.id) == []
    assert not any(e.type is EventType.APPROVAL_REQUESTED for e in await system.repos.events.list(ACTOR.scope_id, task_id=task.id))
    assert (await system.tasks.get(ACTOR.scope_id, task.id)).usage.tool_calls == 0
    assert await system.repos.usage.open_reservations(task.id) == []
    assert feishu.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "accept_timeout", "business", "forbidden"])
async def test_feishu_credentials_never_leaked(tmp_path, feishu, caplog, mode):
    caplog.set_level(logging.DEBUG)
    system, _ = setup(tmp_path, feishu)
    if mode == "accept_timeout":
        task, action = await timeout(system, feishu)
        system.clock.advance(1)
        await system.scheduler.recovery.tick(system.clock())
        stored, = await system.repos.actions.list_for_task(task.id)
        assert stored.receipt_ref == feishu.messages[action.id]
    else:
        task, _, execution = await approved(system)
        feishu.mode = mode
        outcome = await system.gateway.invoke(execution, CALL)
        assert outcome.status is (ToolStatus.SUCCEEDED if mode == "success" else ToolStatus.FAILED)
    events = await system.repos.events.list(ACTOR.scope_id, task_id=task.id)
    trace = await build_trace(system.repos, system.artifacts, ACTOR.scope_id, task.id)
    files = [p.read_bytes() for p in system.config.artifact_root.rglob("*") if p.is_file()]
    assert files and events and trace
    buffers = files + [str(x).encode() for x in (events, trace, caplog.text, system.config.safe_dict(), repr(system.config))]
    for value in (feishu.secret, feishu.token, feishu.app_id):
        assert all(value.encode() not in item for item in buffers)
    assert "R4_FIXTURE_APP_SECRET" in repr(system.config)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("valid_receipt", [True, False])
@pytest.mark.parametrize("submission", ["submit_result", "required_action_receipts"])
async def test_feishu_capability_end_to_end(tmp_path, feishu, valid_receipt, submission, monkeypatch):
    after_commit = []
    write_baseline = BaselineWriter.write

    async def observed(writer, task, outcome):
        await write_baseline(writer, task, outcome)
        after_commit.append((task.id, outcome.status))

    monkeypatch.setattr(BaselineWriter, "write", observed)

    def receipt(ctx):
        message, = (m["content"] for m in ctx.messages if m.get("role") == "tool" and "fixture-message-" in m["content"])
        return re.search(r"fixture-message-[a-f0-9]{32}", message)[0]

    def finish(ctx):
        ref = receipt(ctx) if valid_receipt else "fixture-invented-receipt"
        artifacts = {"receipt": written_id(ctx, "receipt")}
        if submission == "submit_result":
            call = ToolCallPart(id="submit", name=SUBMIT_RESULT, args={
                "summary": "sent", "artifacts": artifacts, "evidence": [ref],
            })
            return build_step(model="scripted", protocol="scripted", text="", calls=[call], usage=Usage())
        step = result_step("sent", artifacts)
        step.candidate.required_action_receipts.append(ref)
        return step

    script = [tool_step((TOOL, ARGS)), lambda ctx: tool_step(("artifacts.write", {
        "name": "receipt", "content": receipt(ctx),
    })), finish]
    system, model = setup(tmp_path, feishu, script=script)
    task = await create(system, checks=("artifact_readable", "feishu_receipt"))
    worker = system.worker()
    assert (await worker.run_once()).status is TaskStatus.WAITING
    assert feishu.calls == []
    action = await approve(system, task)
    report = await worker.run_once()
    verification, = await system.repos.verifications.list_for_task(task.id)
    evidence, = (c for c in verification.checks if c.id == "evidence_refs")
    receipt_check, = (c for c in verification.checks if c.id == "feishu_receipt")
    assert receipt_check.status is evidence.status
    assert (task.id, report.status) in after_commit
    assert report.committed and len(feishu.messages) == 1
    if valid_receipt:
        assert report.status is TaskStatus.SUCCEEDED and evidence.status is VerificationStatus.PASSED
        assert set(evidence.evidence_refs) == {feishu.messages[action.id]}
        assert (await system.artifacts.read(report.result_ref, ACTOR)).content.decode() == feishu.messages[action.id]
    else:
        assert report.status is TaskStatus.FAILED and evidence.status is VerificationStatus.FAILED
    assert all(value not in str(model.calls) for value in (feishu.app_id, feishu.secret, feishu.token))


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "expected"), [
    ("disconnect", "unknown"), ("busy", "unknown"), ("throttled", "unknown"), ("malformed", "unknown"),
    ("no_receipt", "unknown"), ("forbidden", "unknown"), ("business", "unknown"), ("bot_removed", "unknown"),
    ("group_throttle", "unknown"), ("api_throttle", "unknown"), ("unrecognized", "unknown"),
    ("missing_data", "unknown"), ("invalid_code", "unknown"), ("bad_http", "unknown"), ("redirect", "unknown"),
    ("in_flight", "unknown"), ("invalid_target", "unknown"),
])
async def test_reconciliation_classifies_remote_response(tmp_path, feishu, mode, expected):
    system, _ = setup(tmp_path, feishu)
    task, _ = await timeout(system, feishu)
    feishu.mode = mode
    system.clock.advance(1)
    await system.scheduler.recovery.tick(system.clock())
    events = await system.repos.events.list(ACTOR.scope_id, task_id=task.id)
    assert [e.payload["status"] for e in events if e.type is EventType.TOOL_RECONCILE] == [expected]
    assert len(feishu.calls) == 4


@pytest.mark.asyncio
async def test_legacy_action_and_expiry_during_token_request(tmp_path, feishu):
    system, _ = setup(tmp_path, feishu)
    connector = connectors.FeishuSendConnector(system.config, system.artifacts, system.clock)
    legacy = ActionRef(action_id="legacy", task_id="task", logical_key="step", tool=TOOL, request_hash="hash")
    result = await connector.reconcile(legacy)
    assert (result.status, result.detail, feishu.calls) == ("unsupported", WINDOW, [])
    task, _ = await timeout(system, feishu)
    system.clock.advance(3299)
    feishu.auth_hook = lambda: system.clock.advance(1)
    await system.scheduler.recovery.tick(system.clock())
    events = await system.repos.events.list(ACTOR.scope_id, task_id=task.id)
    assert [e.payload["status"] for e in events if e.type is EventType.TOOL_RECONCILE] == ["unsupported"]
    assert [c[0] for c in feishu.calls] == ["auth", "send", "auth"]


@pytest.mark.asyncio
async def test_reconcile_requires_scoped_approved_arguments(tmp_path, feishu):
    system, _ = setup(tmp_path, feishu)
    _, action, _ = await approved(system)
    connector = connectors.FeishuSendConnector(system.config, system.artifacts, system.clock)
    reference = ActionRef(action_id=action.id, task_id=action.task_id, tool=TOOL, logical_key=action.logical_key,
                          request_hash=action.request_hash, scope_id=ACTOR.scope_id,
                          created_at=action.created_at, args_ref=action.args_ref)
    for change in ({"scope_id": ""}, {"args_ref": None}, {"request_hash": "different"}):
        assert (await connector.reconcile(reference.model_copy(update=change))).status == "unsupported"
    with pytest.raises(NotFound):
        await connector.reconcile(reference.model_copy(update={"scope_id": "other-scope"}))
    times = iter([action.created_at, action.created_at + timedelta(minutes=55)])
    connector = connectors.FeishuSendConnector(system.config, system.artifacts, lambda: next(times))
    assert (await connector.reconcile(reference)).detail == WINDOW
    connector = connectors.FeishuSendConnector(system.config, system.artifacts, system.clock)
    system.clock.advance(-1)
    assert (await connector.reconcile(reference)).detail == WINDOW
    assert feishu.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_reply", [(403, {"code": 1}), (503, {}), (200, {"code": 1}),
                                       (200, {"code": 0}), (200, b"bad-json")])
async def test_reconcile_auth_failure_cannot_decide_prior_send(tmp_path, feishu, auth_reply):
    system, _ = setup(tmp_path, feishu)
    task, _ = await timeout(system, feishu)
    feishu.auth_reply = auth_reply
    system.clock.advance(1)
    await system.scheduler.recovery.tick(system.clock())
    action, = await system.repos.actions.list_for_task(task.id)
    assert action.status is ActionStatus.UNKNOWN and action.receipt_ref is None
    assert [c[0] for c in feishu.calls] == ["auth", "send", "auth"]


@pytest.mark.asyncio
async def test_replay_completes_send_that_never_reached_recipient(tmp_path, feishu):
    system, _ = setup(tmp_path, feishu)
    task, action, execution = await approved(system)
    feishu.mode = "disconnect"
    assert (await system.gateway.invoke(execution, CALL)).status is ToolStatus.UNKNOWN
    assert not feishu.messages
    await park(system, execution, WaitCondition.RECONCILIATION)
    feishu.mode = "success"
    system.clock.advance(1)
    await system.scheduler.recovery.tick(system.clock())
    settled, = await system.repos.actions.list_for_task(task.id)
    assert settled.status is ActionStatus.SUCCEEDED and settled.receipt_ref == feishu.messages[action.id]
    assert feishu.calls[1] == feishu.calls[3] and len(feishu.messages) == 1


@pytest.mark.asyncio
async def test_credentials_resolved_at_request_time(tmp_path, feishu, monkeypatch):
    system, _ = setup(tmp_path, feishu)
    _, _, execution = await approved(system)
    monkeypatch.delenv("R4_FIXTURE_APP_SECRET")
    result = await system.gateway.invoke(execution, CALL)
    assert result.status is ToolStatus.FAILED and feishu.calls == []
    feishu.secret = uuid4().hex
    monkeypatch.setenv("R4_FIXTURE_APP_SECRET", feishu.secret)
    assert (await system.gateway.invoke(execution, CALL)).status is ToolStatus.SUCCEEDED


def test_feishu_timeout_upper_bound():
    assert HarnessConfig(feishu_timeout_sec=60.0).feishu_timeout_sec == 60.0
    for seconds in (60.1, 301.0):
        with pytest.raises(ValueError, match="feishu_timeout_sec"):
            HarnessConfig(feishu_timeout_sec=seconds)


def test_feishu_config_and_policy(tmp_path, feishu):
    default = HarnessConfig.from_env({})
    assert default.feishu_base_url == "https://open.feishu.cn"
    assert default.feishu_allowed_recipients == () and default.feishu_timeout_sec == 10.0
    assert default.feishu_app_id_env == "DARAOS_HARNESS_FEISHU_APP_ID"
    assert default.feishu_app_secret_env == "DARAOS_HARNESS_FEISHU_APP_SECRET"
    env = {"DARAOS_HARNESS_FEISHU_" + key: value for key, value in {
        "BASE_URL": feishu.url, "TIMEOUT_SEC": "0.5", "APP_ID_ENV": "R4_FIXTURE_APP_ID",
        "APP_SECRET_ENV": "R4_FIXTURE_APP_SECRET", "ALLOWED_RECIPIENTS": " chat_id:room, open_id:person ",
    }.items()}
    cfg = HarnessConfig.from_env(env)
    assert (cfg.feishu_base_url, cfg.feishu_timeout_sec) == (feishu.url, 0.5)
    assert (cfg.feishu_app_id_env, cfg.feishu_app_secret_env) == ("R4_FIXTURE_APP_ID", "R4_FIXTURE_APP_SECRET")
    assert cfg.feishu_allowed_recipients == ("chat_id:room", "open_id:person")
    assert cfg.safe_dict()["feishu_credential_env_names"] == ("R4_FIXTURE_APP_ID", "R4_FIXTURE_APP_SECRET")
    for value in ("0", "-1", "nan", "inf", "text"):
        with pytest.raises(ValueError):
            HarnessConfig.from_env({"DARAOS_HARNESS_FEISHU_TIMEOUT_SEC": value})
    for value in ("room", ":room", "chat_id:"):
        with pytest.raises(ValueError):
            HarnessConfig.from_env({"DARAOS_HARNESS_FEISHU_ALLOWED_RECIPIENTS": value})
    for url in ("file://test", "http://", feishu.url + "?private=x", feishu.url + "#fragment",
                feishu.url.replace("://", "://user:password@")):
        with pytest.raises(ValueError, match="feishu_base_url"):
            replace(cfg, feishu_base_url=url)
    system, _ = setup(tmp_path, feishu)
    assert not system.policy.check_recipient(TOOL, TARGET, scope_id="missing", role=MAIN.agent_role).allowed
    assert not system.policy.check_recipient("other", TARGET, scope_id=ACTOR.scope_id, role=MAIN.agent_role).allowed
    path = tmp_path / "bad-policy.yaml"
    path.write_text("users: {s1: {main: {recipients: single}}}", encoding="utf-8")
    with pytest.raises(PolicyFileError, match="list of strings"):
        load_policy_file(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["forbidden", "business", "bot_removed", "invalid_target", "bad_http"])
async def test_execute_retains_definitive_rejections(tmp_path, feishu, mode):
    system, _ = setup(tmp_path, feishu)
    _, _, execution = await approved(system)
    feishu.mode = mode
    outcome = await system.gateway.invoke(execution, CALL)
    assert outcome.status is ToolStatus.FAILED and not outcome.receipt_ref


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "reason"), [
    ("cancelled", "task_cancelled"), ("cancelling", "task_cancelled"), ("cancel_requested", "task_cancelled"),
    ("disabled", "policy_revoked"), ("profile", "policy_revoked"), ("role", "policy_revoked"),
    ("user_tools", "policy_revoked"), ("capability", "policy_revoked"),
    ("deployment_recipient", "recipient_revoked"), ("user_recipient", "recipient_revoked"),
    ("missing_ref", "args_unavailable"), ("unreadable", "args_unavailable"), ("hash", "args_unavailable"),
])
async def test_feishu_reconcile_blocked_when_cancelled_or_policy_revoked(tmp_path, feishu, change, reason, monkeypatch):
    system, _ = setup(tmp_path, feishu)
    task, _ = await timeout(system, feishu)
    task = await system.tasks.get(ACTOR.scope_id, task.id)
    action, = await system.repos.actions.list_for_task(task.id)
    policy = system.policy
    users = policy.users[task.scope_id]
    if change == "cancelled":
        task = await system.tasks.request_cancel(task.scope_id, task.id, "stop")
    elif change == "cancelling":
        task = task.model_copy(update={"status": TaskStatus.CANCELLING})
    elif change == "cancel_requested":
        task = task.model_copy(update={"cancel_requested_at": system.clock()})
    elif change == "disabled":
        policy.disabled_tools = {TOOL}
    elif change == "profile":
        monkeypatch.delitem(system.gateway._profiles, task.agent_profile_ref)
    elif change == "role":
        monkeypatch.setitem(policy.roles, "main_tools@1", frozenset())
    elif change == "user_tools":
        monkeypatch.setitem(users, "main", replace(users["main"], tools=frozenset()))
    elif change == "capability":
        monkeypatch.setitem(policy.capabilities, task.capability_ref, frozenset())
    elif change == "deployment_recipient":
        monkeypatch.setitem(policy.allowed_recipients, TOOL, frozenset())
    elif change == "user_recipient":
        monkeypatch.setitem(users, "main", replace(users["main"], recipients=frozenset()))
    else:
        updates = {"missing_ref": {"args_ref": None}, "unreadable": {"args_ref": "missing"}, "hash": {"request_hash": "wrong"}}
        action = action.model_copy(update=updates[change])
    connector = system.gateway._connectors[TOOL]
    reconcile = AsyncMock(wraps=connector.reconcile)
    monkeypatch.setattr(connector, "reconcile", reconcile)
    before = list(feishu.calls)
    await system.gateway.reconcile(task, action, 1)
    events = await system.repos.events.list(task.scope_id, task_id=task.id)
    history = [e for e in events if e.type is EventType.TOOL_RECONCILE]
    event, = history
    assert (event.payload["status"], event.payload["reason"]) == ("unsupported", reason)
    assert needs_manual_confirmation(history) and feishu.calls == before
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["not-json", "no_text", "no_recipient", "non_object", "wrong_type"])
async def test_broken_arguments_do_not_interrupt_next_recovery(tmp_path, feishu, fault, monkeypatch):
    system, _ = setup(tmp_path, feishu)
    damaged = await create(system, key="damaged")
    lease = await claim(system)
    args = dict(ARGS)
    content = b"{"
    if fault != "not-json":
        if fault == "no_text":
            del args["text"]
        elif fault == "no_recipient":
            del args["receive_id"]
        elif fault == "wrong_type":
            args["text"] = ["invalid"]
        content = json.dumps([] if fault == "non_object" else args).encode()
    ref = await system.artifacts.put(ArtifactInput(scope_id=ACTOR.scope_id, content=content, media_type="application/json"))
    action = Action(task_id=damaged.id, attempt_id=lease.lease.attempt_id, tool=TOOL, logical_key="damaged",
                    request_hash=ToolCall(tool=TOOL, args=args).request_hash(), args_ref=ref.id, read_only=False,
                    status=ActionStatus.UNKNOWN, created_at=system.clock(), updated_at=system.clock())
    await system.repos.actions.register(action)
    await park(system, lease, WaitCondition.RECONCILIATION)
    reference = ActionRef(action_id=action.id, scope_id=damaged.scope_id, **action.model_dump(include={
        "task_id", "tool", "logical_key", "request_hash", "args_ref", "created_at",
    }))
    connector = connectors.FeishuSendConnector(system.config, system.artifacts, system.clock)
    assert (await connector.reconcile(reference)).status == "unsupported"
    assert feishu.calls == []
    good, approved_action = await timeout(system, feishu, "good")
    # Recovery normally visits tasks by random ID. Exercise the damaged one first.
    candidates = await system.repos.tasks.list_reconciliation_candidates()
    assert {t.id for t in candidates} == {damaged.id, good.id}
    ordered = sorted(candidates, key=lambda t: t.id != damaged.id)
    monkeypatch.setattr(system.repos.tasks, "list_reconciliation_candidates", AsyncMock(return_value=ordered))
    system.clock.advance(1)
    await system.scheduler.recovery.tick(system.clock())
    events = await system.repos.events.list(damaged.scope_id, task_id=damaged.id)
    event, = (e for e in events if e.type is EventType.TOOL_RECONCILE)
    assert (event.payload["status"], event.payload["reason"]) == ("unsupported", "args_unavailable")
    settled, = await system.repos.actions.list_for_task(good.id)
    assert settled.status is ActionStatus.SUCCEEDED and settled.receipt_ref == feishu.messages[approved_action.id]
    assert [c[0] for c in feishu.calls] == ["auth", "send", "auth", "send"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auth_disconnect", "auth_timeout"])
@pytest.mark.parametrize("replaying", [False, True])
async def test_token_transport_failure_distinguishes_execute_from_reconcile(tmp_path, feishu, mode, replaying):
    system, _ = setup(tmp_path, feishu)
    if replaying:
        task, _ = await timeout(system, feishu)
        before = list(feishu.calls)
        feishu.mode = mode
        system.clock.advance(1)
        await system.scheduler.recovery.tick(system.clock())
        events = await system.repos.events.list(task.scope_id, task_id=task.id)
        assert [e.payload["status"] for e in events if e.type is EventType.TOOL_RECONCILE] == ["unknown"]
        assert feishu.calls == before + [("auth",)]
    else:
        _, _, execution = await approved(system)
        feishu.mode = mode
        outcome = await system.gateway.invoke(execution, CALL)
        assert outcome.status is ToolStatus.FAILED and feishu.calls == [("auth",)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "checks"), [
    ("omitted", ("artifact_readable",)), ("omitted", ("artifact_readable", "feishu_receipt")),
    pytest.param("artifact_only", ("artifact_readable",), id="legacy-artifact-as-evidence"),
    ("invented", ("feishu_receipt",)), ("artifact_only", ("feishu_receipt",)),
    ("mismatched_text", ("feishu_receipt",)), ("unreadable", ("feishu_receipt",)),
    ("other_task", ("feishu_receipt",)), ("success", ("feishu_receipt",)),
])
async def test_feishu_receipt_checker(tmp_path, feishu, case, checks):
    system, _ = setup(tmp_path, feishu)
    if case in {"mismatched_text", "unreadable", "other_task", "success"}:
        original, _, execution = await approved(system, checks=checks)
        sent = await system.gateway.invoke(execution, CALL)
        assert sent.status is ToolStatus.SUCCEEDED
        receipt = sent.receipt_ref
        task = await create(system, key="other", checks=checks) if case == "other_task" else original
    elif "feishu_receipt" not in checks:
        # Persist a legacy task directly, bypassing TaskService's creation checks.
        task = Task.new(task_request("legacy", checks), ACTOR, MAIN, now=system.clock())
        await system.repos.tasks.create(task)
        receipt = "fixture-fabricated"
    else:
        task = await create(system, checks=checks)
        receipt = "fixture-fabricated"
    content = "different" if case == "mismatched_text" else receipt
    artifact = await system.artifacts.put(ArtifactInput(scope_id=task.scope_id, task_id=task.id, name="receipt",
                                          media_type="text/plain", content=content.encode()))
    artifact_id = "missing-artifact" if case == "unreadable" else artifact.id
    cited = [] if case == "omitted" else [artifact.id if case == "artifact_only" else receipt]
    candidate = CandidateResult(summary="done", artifact_refs={"receipt": artifact_id}, evidence_refs=cited)
    result = await system.verifier.verify(candidate, task)
    assert result.status is (VerificationStatus.PASSED if case == "success" else VerificationStatus.FAILED)
    results = {c.id: c for c in result.checks}
    if case == "omitted":
        assert results["result_schema"].status is VerificationStatus.FAILED
    receipt_check, = (c for c in result.checks if c.id == "feishu_receipt")
    expected = VerificationStatus.PASSED if case == "success" else VerificationStatus.FAILED
    assert receipt_check.status is expected
    if case == "artifact_only" and "feishu_receipt" not in checks:
        assert all(c.status is VerificationStatus.PASSED for c in result.checks if c.id != "feishu_receipt")
        assert feishu.calls == []
    if case == "success":
        assert receipt_check.evidence_refs == [receipt]
