"""TaskService (tasks/service.py) and the permissions file (app.py)."""

import pytest

from harness.agents import AgentRegistry
from harness.app import PolicyFileError, load_policy_file, user_grants
from harness.capabilities import CapabilityRegistry
from harness.config import DeploymentCaps, HarnessConfig
from harness.contracts import (
    Acceptance,
    Actor,
    AgentRole,
    Checkpoint,
    IdempotencyConflict,
    Limits,
    LimitsExceeded,
    NotFound,
    RunLane,
    StaleLease,
    TaskOrigin,
    TaskOutcome,
    TaskRequest,
    TaskStatus,
    TriggerType,
)
from harness.results import FileArtifactStore
from harness.storage.memory import InMemoryRepositories
from harness.tasks import TaskService, UnknownCheck
from harness.tools import WhitelistPolicy

from ..fakes import Clock

ME = Actor(actor_id="u1", scope_id="s1")
MAIN = TaskOrigin(agent_role=AgentRole.MAIN, agent_profile_ref="main@1", run_lane=RunLane.FOREGROUND,
                  trigger_type=TriggerType.USER)


def request(**kw):
    data = dict(
        request_key="k1",
        goal="draft",
        capability_ref="document_draft@1",
        acceptance=Acceptance(required_artifacts=["draft"], checks=[{"id": "artifact_readable"}]),
    )
    data.update(kw)
    return TaskRequest(**data)


def service(tmp_path, **config):
    repos = InMemoryRepositories()
    clock = Clock()
    svc = TaskService(
        repos.tasks,
        HarnessConfig(**config),
        artifacts=FileArtifactStore(tmp_path / "artifacts", repos.artifacts),
        capabilities=CapabilityRegistry.builtin(),
        agents=AgentRegistry.builtin(),
        clock=clock,
        after_commit=[],
    )
    return svc, repos, clock


@pytest.mark.asyncio
async def test_create_and_idempotency(tmp_path):
    svc, _, _ = service(tmp_path)
    a = await svc.create(request(), ME, MAIN)
    again = await svc.create(request(), ME, MAIN)
    assert again.id == a.id and a.status is TaskStatus.QUEUED
    with pytest.raises(IdempotencyConflict):
        await svc.create(request(goal="other"), ME, MAIN)


@pytest.mark.asyncio
async def test_create_refuses_limits_over_the_caps(tmp_path):
    svc, _, _ = service(tmp_path, caps=DeploymentCaps(max_model_calls=10), default_limits=Limits(max_model_calls=5))
    with pytest.raises(LimitsExceeded):
        await svc.create(request(limits=Limits(max_model_calls=11)), ME, MAIN)


@pytest.mark.asyncio
async def test_create_refuses_unknown_capability_profile_or_check(tmp_path):
    svc, _, _ = service(tmp_path)
    with pytest.raises(NotFound):
        await svc.create(request(capability_ref="spreadsheet@1"), ME, MAIN)
    with pytest.raises(NotFound):
        await svc.create(request(), ME, MAIN.model_copy(update={"agent_profile_ref": "main@9"}))
    with pytest.raises(UnknownCheck, match="facts_match"):
        await svc.create(request(acceptance=Acceptance(checks=[{"id": "facts_match"}])), ME, MAIN)


@pytest.mark.asyncio
async def test_claim_heartbeat_commit_and_stale_lease(tmp_path):
    svc, _, clock = service(tmp_path, lease_ttl_sec=60, heartbeat_interval_sec=15)
    task = await svc.create(request(), ME, MAIN)
    lease = await svc.claim("w1", [RunLane.FOREGROUND])
    assert lease.task_id == task.id
    assert await svc.claim("w2", [RunLane.FOREGROUND]) is None
    clock.advance(50)
    await svc.heartbeat(lease)  # extends the lease past the first 60 s
    clock.advance(50)
    await svc.heartbeat(lease)
    clock.advance(61)
    with pytest.raises(StaleLease):
        await svc.heartbeat(lease)


@pytest.mark.asyncio
async def test_claim_needs_a_lane(tmp_path):
    svc, _, _ = service(tmp_path)
    with pytest.raises(ValueError):
        await svc.claim("w1", [])


@pytest.mark.asyncio
async def test_checkpoint_is_stored_and_referenced(tmp_path):
    svc, repos, _ = service(tmp_path)
    task = await svc.create(request(), ME, MAIN)
    lease = await svc.claim("w1", [RunLane.FOREGROUND])
    await svc.checkpoint(lease, Checkpoint(stage="drafting", step_id="step-3", completed_action_ids=["act_1"]))
    stored = await svc.get("s1", task.id)
    assert stored.checkpoint_ref
    art = await repos.artifacts.get("s1", stored.checkpoint_ref)
    assert art.task_id is None and art.name == f"{task.id}/checkpoint/step-3"


@pytest.mark.asyncio
async def test_cancel_and_commit(tmp_path):
    svc, _, _ = service(tmp_path)
    task = await svc.create(request(), ME, MAIN)
    lease = await svc.claim("w1", [RunLane.FOREGROUND])
    assert (await svc.request_cancel("s1", task.id, "user")).status is TaskStatus.CANCELLING
    done = await svc.commit(lease, TaskOutcome(status=TaskStatus.CANCELLED))
    assert done.status is TaskStatus.CANCELLED


# -- permissions file -------------------------------------------------------------------


def test_policy_file_round_trip(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(
        "disabled_tools: [artifacts.write]\n"
        "users:\n  s1:\n    main:\n      paths: [/data/s1]\n      tools: [files.read, artifacts.write]\n",
        encoding="utf-8",
    )
    spec = load_policy_file(path)
    grants = user_grants(spec)
    assert grants["s1"]["main"].paths == ("/data/s1",)
    assert grants["s1"]["main"].tools == {"files.read", "artifacts.write"}
    assert grants["s1"]["main"].ref == "grant:s1:main"
    policy = WhitelistPolicy(
        roles={"main_tools@1": {"files.read", "artifacts.read", "artifacts.write"}},
        users=grants,
        disabled_tools=spec["disabled_tools"],
    )
    assert policy.allowed_tools(scope_id="s1", role="main", tool_policy_ref="main_tools@1", capability_ref=None) == {"files.read"}
    d = policy.check_tool("artifacts.write", scope_id="s1", role="main", tool_policy_ref="main_tools@1", capability_ref=None)
    assert not d.allowed and "disabled in this deployment" in d.reason


def test_no_policy_file_means_no_users():
    assert load_policy_file(None) == {"disabled_tools": [], "users": {}, "tokens": []}


@pytest.mark.parametrize(
    "body,match",
    [
        ("surprise: 1\n", "unknown keys"),
        ("disabled_tools: files.read\n", "disabled_tools"),
        ("users:\n  s1:\n    boss: {}\n", "unknown role"),
        ("users:\n  s1:\n    main:\n      paths: /data\n", "paths"),
        ("users:\n  s1:\n    main:\n      everything: true\n", "allows only"),
        ("- a\n", "mapping"),
    ],
)
def test_bad_policy_files_are_refused(tmp_path, body, match):
    path = tmp_path / "policy.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(PolicyFileError, match=match):
        load_policy_file(path)


def test_missing_policy_file_is_an_error(tmp_path):
    with pytest.raises(PolicyFileError, match="not found"):
        load_policy_file(tmp_path / "nope.yaml")


def test_token_file_validation(tmp_path):
    from harness.api import token_digest

    good = tmp_path / "ok.yaml"
    good.write_text(f"tokens:\n  - sha256: {token_digest('t')}\n    scope_id: s1\n    actor_id: u1\n", encoding="utf-8")
    assert load_policy_file(good)["tokens"][0]["scope_id"] == "s1"
    for body, match in (
        ("tokens: abc\n", "must be a list"),
        ("tokens:\n  - sha256: short\n    scope_id: s\n    actor_id: u\n", "64 hex"),
        ("tokens:\n  - token: plain\n    scope_id: s\n    actor_id: u\n", "needs exactly"),
        (f"tokens:\n  - {{sha256: {token_digest('t')}, scope_id: s, actor_id: u}}\n  - {{sha256: {token_digest('t')}, scope_id: s2, actor_id: v}}\n", "repeats"),
    ):
        bad = tmp_path / "bad.yaml"
        bad.write_text(body, encoding="utf-8")
        with pytest.raises(PolicyFileError, match=match):
            load_policy_file(bad)
