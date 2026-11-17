"""ToolGateway (tools/gateway.py), policy (tools/policy.py) and the P0 connectors."""

import pytest

from harness.contracts import (
    ActionConflict,
    ActionStatus,
    AgentRole,
    ErrorCode,
    EventType,
    Limits,
    ReconcileResult,
    StaleLease,
    TaskOutcome,
    TaskStatus,
    ToolCall,
    ToolResult,
    ToolSpec,
    ToolStatus,
)
from harness.storage.memory import InMemoryRepositories
from harness.tools import PolicyToolCatalog, ToolGateway, UserGrant, WhitelistPolicy
from harness.tools.connectors import p0_connectors

from ..fakes import ACTOR, MAIN_PROFILE, T0, TTL, Clock, MemoryArtifactStore, claimed_task

pytestmark = pytest.mark.asyncio

ALL = {"files.read", "artifacts.read", "artifacts.write"}


def make_policy(tmp_path, *, user_tools=None, roles=None, capabilities=None, users=None):
    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)
    return WhitelistPolicy(
        roles=roles or {"main_tools@1": ALL, "proactive_tools@1": {"files.read", "artifacts.read"}},
        users=users
        if users is not None
        else {ACTOR.scope_id: {AgentRole.MAIN: UserGrant(tools=user_tools, paths=(str(allowed),), ref="grant:s1:main")}},
        capabilities=capabilities or {},
    )


class Setup:
    def __init__(self, tmp_path, *, policy=None, limits=None, input_refs=None, connectors=None):
        self.tmp = tmp_path
        self.repos = InMemoryRepositories()
        self.clock = Clock()
        self.store = MemoryArtifactStore(self.repos)
        self.policy = policy or make_policy(tmp_path)
        self.gateway = ToolGateway(
            self.repos,
            self.store,
            self.policy,
            connectors if connectors is not None else p0_connectors(self.store),
            profiles={"main@1": MAIN_PROFILE.tool_policy_ref},
            clock=self.clock,
        )
        self.limits = limits
        self.input_refs = input_refs or []

    async def start(self):
        self.task, self.execution = await claimed_task(self.repos, limits=self.limits)
        if self.input_refs:
            stored = self.repos.tasks._s.tasks[self.task.id]
            self.repos.tasks._s.tasks[self.task.id] = stored.model_copy(update={"input_refs": self.input_refs})
            self.task = await self.repos.tasks.get("s1", self.task.id)
        return self

    async def call(self, tool, key="step-1.0", **args):
        return await self.gateway.invoke(self.execution, ToolCall(tool=tool, args=args, logical_key=key))

    async def usage(self):
        return (await self.repos.tasks.get("s1", self.task.id)).usage

    async def actions(self):
        return await self.repos.actions.list_for_task(self.task.id)


def write_file(setup, name="notes.md", text="line1\nline2\nline3\n"):
    path = setup.tmp / "allowed" / name
    path.write_text(text, encoding="utf-8")
    return path


# -- the happy path -------------------------------------------------------------


async def test_read_file_inside_the_whitelist(tmp_path):
    s = await Setup(tmp_path).start()
    path = write_file(s)
    outcome = await s.call("files.read", path=str(path))
    assert outcome.status is ToolStatus.SUCCEEDED
    assert "line2" in outcome.summary and outcome.action_id
    (action,) = await s.actions()
    assert action.status is ActionStatus.SUCCEEDED and action.read_only
    assert action.grant_ref == "grant:s1:main"
    assert action.args_ref and action.result_ref
    assert (await s.usage()).tool_calls == 1
    assert await s.repos.usage.open_reservations(s.task.id) == []


async def test_write_then_read_an_artifact(tmp_path):
    s = await Setup(tmp_path).start()
    wrote = await s.call("artifacts.write", key="step-1.0", name="draft", content="# Draft\nhello")
    assert wrote.status is ToolStatus.SUCCEEDED and len(wrote.artifact_refs) == 1
    art_id = wrote.artifact_refs[0]
    work = await s.repos.artifacts.list_for_task("s1", s.task.id)
    assert [a.name for a in work] == ["draft"]  # trajectory JSON is not a work artifact
    read = await s.call("artifacts.read", key="step-2.0", artifact_id=art_id)
    assert read.status is ToolStatus.SUCCEEDED and "hello" in read.summary
    by_tool = {a.tool: a for a in await s.actions()}
    assert not by_tool["artifacts.write"].read_only and by_tool["artifacts.read"].read_only


async def test_tool_action_event_is_recorded(tmp_path):
    s = await Setup(tmp_path).start()
    await s.call("files.read", path=str(write_file(s)))
    events = [e for e in await s.repos.events.list("s1", task_id=s.task.id) if e.type is EventType.TOOL_ACTION]
    assert len(events) == 1
    p = events[0].payload
    assert p["tool"] == "files.read" and p["status"] == "succeeded" and p["read_only"] is True
    assert s.store.json(p["args_ref"])["path"].endswith("notes.md")


# -- permissions: role ∩ user ∩ capability -----------------------------------------


async def test_tool_outside_the_role_whitelist_is_denied(tmp_path):
    policy = make_policy(tmp_path, roles={"main_tools@1": {"files.read"}})
    s = await Setup(tmp_path, policy=policy).start()
    outcome = await s.call("artifacts.write", name="d", content="x")
    assert outcome.status is ToolStatus.DENIED and outcome.action_id is None
    assert outcome.error.code is ErrorCode.FORBIDDEN and "role" in outcome.error.message
    assert await s.actions() == [] and (await s.usage()).tool_calls == 0


async def test_user_whitelist_narrows_but_cannot_widen(tmp_path):
    narrowed = make_policy(tmp_path, user_tools=frozenset({"files.read"}))
    s = await Setup(tmp_path, policy=narrowed).start()
    assert (await s.call("artifacts.write", name="d", content="x")).status is ToolStatus.DENIED
    # A user entry naming a tool the role does not have does not grant it.
    widened = make_policy(
        tmp_path,
        roles={"main_tools@1": {"files.read"}},
        user_tools=frozenset({"files.read", "artifacts.write"}),
    )
    s2 = await Setup(tmp_path, policy=widened).start()
    assert (await s2.call("artifacts.write", name="d", content="x")).status is ToolStatus.DENIED


async def test_user_without_an_entry_gets_nothing(tmp_path):
    s = await Setup(tmp_path, policy=make_policy(tmp_path, users={})).start()
    outcome = await s.call("files.read", path=str(write_file(s)))
    assert outcome.status is ToolStatus.DENIED and "not granted" in outcome.error.message


async def test_other_users_entry_does_not_apply(tmp_path):
    other = {"someone-else": {AgentRole.MAIN: UserGrant(paths=(str(tmp_path),))}}
    s = await Setup(tmp_path, policy=make_policy(tmp_path, users=other)).start()
    assert (await s.call("files.read", path=str(write_file(s)))).status is ToolStatus.DENIED


async def test_capability_limits_tools(tmp_path):
    policy = make_policy(tmp_path, capabilities={"document_draft@1": {"files.read"}})
    s = await Setup(tmp_path, policy=policy).start()
    assert (await s.call("files.read", path=str(write_file(s)))).status is ToolStatus.SUCCEEDED
    denied = await s.call("artifacts.write", key="step-2.0", name="d", content="x")
    assert denied.status is ToolStatus.DENIED and "capability" in denied.error.message


async def test_catalog_shows_only_the_intersection(tmp_path):
    store = MemoryArtifactStore(InMemoryRepositories())
    specs = [c.spec for c in p0_connectors(store)]
    policy = make_policy(tmp_path, user_tools=frozenset({"files.read", "artifacts.write"}),
                         capabilities={"document_draft@1": {"artifacts.write", "artifacts.read"}})
    repos = InMemoryRepositories()
    task, _ = await claimed_task(repos)
    names = [s.name for s in await PolicyToolCatalog(policy, specs).tools_for(task, MAIN_PROFILE)]
    assert names == ["artifacts.write"]


# -- resource scope ---------------------------------------------------------------


async def test_path_outside_the_user_directories_is_denied(tmp_path):
    s = await Setup(tmp_path).start()
    secret = tmp_path / "secret.env"
    secret.write_text("KEY=1")
    outcome = await s.call("files.read", path=str(secret))
    assert outcome.status is ToolStatus.DENIED and "outside" in outcome.error.message
    assert await s.actions() == []


async def test_traversal_out_of_the_directory_is_denied(tmp_path):
    s = await Setup(tmp_path).start()
    (tmp_path / "secret.env").write_text("KEY=1")
    sneaky = tmp_path / "allowed" / ".." / "secret.env"
    assert (await s.call("files.read", path=str(sneaky))).status is ToolStatus.DENIED


async def test_symlink_out_of_the_directory_is_denied(tmp_path):
    s = await Setup(tmp_path).start()
    target = tmp_path / "secret.env"
    target.write_text("KEY=1")
    link = tmp_path / "allowed" / "link.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not available here")
    assert (await s.call("files.read", path=str(link))).status is ToolStatus.DENIED


async def test_input_refs_do_not_widen_the_directories(tmp_path):
    # Review of #16: input_refs come from whoever created the task, so
    # "file:/etc/passwd" in them must not make that file readable.
    named = tmp_path / "secret.env"
    named.write_text("KEY=do-not-read")
    s = await Setup(tmp_path, input_refs=[f"file:{named}"]).start()
    outcome = await s.call("files.read", path=str(named))
    assert outcome.status is ToolStatus.DENIED and outcome.error.code is ErrorCode.FORBIDDEN
    assert await s.actions() == []


async def test_input_file_inside_the_directories_is_read(tmp_path):
    s = await Setup(tmp_path).start()
    named = write_file(s, "brief.md", "the brief\n")
    s.input_refs = [f"file:{named}"]
    outcome = await s.call("files.read", path=str(named))
    assert outcome.status is ToolStatus.SUCCEEDED and "the brief" in outcome.summary


# -- idempotency: (task_id, logical_key) ------------------------------------------------


async def test_same_key_same_args_reuses_the_earlier_success(tmp_path):
    s = await Setup(tmp_path).start()
    path = write_file(s)
    first = await s.call("files.read", path=str(path))
    path.write_text("changed\n")
    again = await s.call("files.read", path=str(path))
    assert again.status is ToolStatus.SUCCEEDED and again.action_id == first.action_id
    assert again.summary == first.summary  # not re-run: still the old content
    assert len(await s.actions()) == 1 and (await s.usage()).tool_calls == 1


async def test_failed_call_is_retried_and_charged_again(tmp_path):
    # Review of #16: a retry under the same key must count against
    # max_tool_calls, or the limit does not bound retries.
    s = await Setup(tmp_path, limits=Limits(max_tool_calls=2)).start()
    missing = tmp_path / "allowed" / "later.md"
    first = await s.call("files.read", path=str(missing))
    assert first.status is ToolStatus.FAILED
    missing.write_text("now here\n")
    second = await s.call("files.read", path=str(missing))
    assert second.status is ToolStatus.SUCCEEDED and second.action_id == first.action_id
    assert (await s.usage()).tool_calls == 2
    third = await s.call("files.read", key="step-2.0", path=str(missing))
    assert third.status is ToolStatus.DENIED and third.error.code is ErrorCode.BUDGET_EXHAUSTED


async def test_retry_beyond_the_budget_is_refused(tmp_path):
    s = await Setup(tmp_path, limits=Limits(max_tool_calls=1)).start()
    missing = tmp_path / "allowed" / "nope.md"
    assert (await s.call("files.read", path=str(missing))).status is ToolStatus.FAILED
    again = await s.call("files.read", path=str(missing))
    assert again.status is ToolStatus.DENIED and again.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert (await s.usage()).tool_calls == 1


async def test_unfinished_write_is_not_run_again(tmp_path):
    # Review of #16: a write still marked running (its worker died mid-call)
    # may already have taken effect; running it again could do it twice.
    s = await Setup(tmp_path).start()
    first = await s.call("artifacts.write", name="draft", content="v1")
    assert first.status is ToolStatus.SUCCEEDED
    (action,) = await s.actions()
    await s.repos.actions.update(action.id, ActionStatus.RUNNING, now=s.clock())
    again = await s.call("artifacts.write", name="draft", content="v1")
    assert again.status is ToolStatus.UNKNOWN and "did not finish" in again.error.message
    assert (await s.usage()).tool_calls == 1


async def test_unfinished_read_is_retried(tmp_path):
    s = await Setup(tmp_path).start()
    path = write_file(s)
    first = await s.call("files.read", path=str(path))
    (action,) = await s.actions()
    await s.repos.actions.update(action.id, ActionStatus.RUNNING, now=s.clock())
    again = await s.call("files.read", path=str(path))
    assert again.status is ToolStatus.SUCCEEDED and again.action_id == first.action_id
    assert (await s.usage()).tool_calls == 2


async def test_same_key_other_args_is_a_conflict(tmp_path):
    s = await Setup(tmp_path).start()
    await s.call("files.read", path=str(write_file(s, "a.md")))
    with pytest.raises(ActionConflict):
        await s.call("files.read", path=str(write_file(s, "b.md")))


async def test_reused_write_returns_the_same_artifact(tmp_path):
    s = await Setup(tmp_path).start()
    first = await s.call("artifacts.write", name="draft", content="v1")
    again = await s.call("artifacts.write", name="draft", content="v1")
    assert again.artifact_refs == first.artifact_refs
    assert len(await s.repos.artifacts.list_for_task("s1", s.task.id)) == 1


# -- budget, lease, cancellation, deadline ----------------------------------------------


async def test_tool_budget_used_up_is_denied_before_running(tmp_path):
    s = await Setup(tmp_path, limits=Limits(max_tool_calls=1)).start()
    path = write_file(s)
    assert (await s.call("files.read", key="step-1.0", path=str(path))).status is ToolStatus.SUCCEEDED
    outcome = await s.call("files.read", key="step-2.0", path=str(path))
    assert outcome.status is ToolStatus.DENIED and outcome.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert len(await s.actions()) == 1


async def test_stale_lease_raises(tmp_path):
    s = await Setup(tmp_path).start()
    await s.repos.tasks.commit(s.execution.lease, TaskOutcome(status=TaskStatus.QUEUED), T0)
    await s.repos.tasks.claim("w2", [s.task.run_lane], TTL, T0)
    with pytest.raises(StaleLease):
        await s.call("files.read", path=str(write_file(s)))


async def test_cancel_requested_denies_new_calls(tmp_path):
    s = await Setup(tmp_path).start()
    await s.repos.tasks.request_cancel("s1", s.task.id, "user", T0)
    outcome = await s.call("files.read", path=str(write_file(s)))
    assert outcome.status is ToolStatus.DENIED and outcome.error.code is ErrorCode.CANCELLED


async def test_deadline_passed_denies_new_calls(tmp_path):
    s = await Setup(tmp_path, limits=Limits(timeout_sec=60)).start()
    s.clock.advance(61)
    outcome = await s.call("files.read", path=str(write_file(s)))
    assert outcome.status is ToolStatus.DENIED and outcome.error.code is ErrorCode.DEADLINE_EXCEEDED


async def test_missing_logical_key_is_a_programming_error(tmp_path):
    s = await Setup(tmp_path).start()
    with pytest.raises(ValueError):
        await s.gateway.invoke(s.execution, ToolCall(tool="files.read", args={"path": "x"}))


# -- argument checks, connector errors ----------------------------------------------------


async def test_bad_arguments_fail_without_running(tmp_path):
    s = await Setup(tmp_path).start()
    outcome = await s.call("artifacts.write", name="d")  # content missing
    assert outcome.status is ToolStatus.FAILED and outcome.error.code is ErrorCode.INVALID_REQUEST
    assert await s.actions() == []


async def test_unknown_tool_is_denied(tmp_path):
    s = await Setup(tmp_path).start()
    assert (await s.call("shell.run", cmd="ls")).status is ToolStatus.DENIED


async def test_missing_file_is_a_failed_action_and_charged(tmp_path):
    s = await Setup(tmp_path).start()
    outcome = await s.call("files.read", path=str(tmp_path / "allowed" / "nope.md"))
    assert outcome.status is ToolStatus.FAILED and outcome.error.code is ErrorCode.NOT_FOUND
    (action,) = await s.actions()
    assert action.status is ActionStatus.FAILED and (await s.usage()).tool_calls == 1


class _Boom:
    def __init__(self, spec):
        self.spec = spec

    async def execute(self, call):
        raise RuntimeError("connector bug")

    async def reconcile(self, action):
        return ReconcileResult(status="unsupported")


READ_ONLY_BOOM = ToolSpec(name="boom.read", description="", read_only=True, side_effects=False)
WRITE_BOOM = ToolSpec(name="boom.write", description="", read_only=False, side_effects=False)


def boom_setup(tmp_path, spec):
    policy = WhitelistPolicy(
        roles={"main_tools@1": {spec.name}},
        users={ACTOR.scope_id: {AgentRole.MAIN: UserGrant()}},
    )
    return Setup(tmp_path, policy=policy, connectors=[_Boom(spec)])


async def test_read_only_connector_crash_is_failed(tmp_path):
    s = await boom_setup(tmp_path, READ_ONLY_BOOM).start()
    outcome = await s.call("boom.read")
    assert outcome.status is ToolStatus.FAILED and "connector bug" in outcome.error.message
    assert await s.repos.usage.open_reservations(s.task.id) == []


async def test_writing_connector_crash_is_unknown_and_keeps_its_reservation(tmp_path):
    s = await boom_setup(tmp_path, WRITE_BOOM).start()
    outcome = await s.call("boom.write")
    assert outcome.status is ToolStatus.UNKNOWN
    (action,) = await s.actions()
    assert action.status is ActionStatus.UNKNOWN
    assert len(await s.repos.usage.open_reservations(s.task.id)) == 1
    # The same call again is not re-run blindly.
    again = await s.call("boom.write")
    assert again.status is ToolStatus.UNKNOWN and again.action_id == action.id


async def test_outside_side_effects_need_approval(tmp_path):
    spec = ToolSpec(name="mail.send", description="", read_only=False, side_effects=True)

    class Mail(_Boom):
        async def execute(self, call):  # pragma: no cover - must never run
            raise AssertionError("ran without approval")

    policy = WhitelistPolicy(roles={"main_tools@1": {"mail.send"}}, users={ACTOR.scope_id: {AgentRole.MAIN: UserGrant()}})
    s = await Setup(tmp_path, policy=policy, connectors=[Mail(spec)]).start()
    outcome = await s.call("mail.send")
    assert outcome.status is ToolStatus.NEEDS_APPROVAL and await s.actions() == []


# -- connectors on their own --------------------------------------------------------------


async def test_long_file_page_is_stored_as_an_artifact(tmp_path):
    s = await Setup(tmp_path).start()
    path = write_file(s, "big.md", "\n".join("x" * 100 for _ in range(200)))
    outcome = await s.call("files.read", path=str(path), limit=200)
    assert outcome.status is ToolStatus.SUCCEEDED and len(outcome.artifact_refs) == 1
    assert "artifacts.read" in outcome.summary


async def test_binary_file_is_refused(tmp_path):
    s = await Setup(tmp_path).start()
    path = tmp_path / "allowed" / "blob.bin"
    path.write_bytes(b"\x00\x01\x02")
    outcome = await s.call("files.read", path=str(path))
    assert outcome.status is ToolStatus.FAILED and "binary" in outcome.error.message


async def test_paging_reports_more(tmp_path):
    s = await Setup(tmp_path).start()
    path = write_file(s, "lines.md", "\n".join(f"l{i}" for i in range(10)))
    outcome = await s.call("files.read", path=str(path), offset=3, limit=2)
    assert "l2\nl3" in outcome.summary and "offset=5" in outcome.summary


async def test_connector_results_never_claim_denied():
    with pytest.raises(ValueError):
        ToolResult(status=ToolStatus.DENIED)


# -- spec 4.4 / 7.4: cancel just before running, and timeouts --------------------------


class _Slow:
    def __init__(self, spec, *, seconds=5.0, on_start=None):
        self.spec = spec
        self.seconds = seconds
        self.on_start = on_start
        self.ran = False

    async def execute(self, call):
        import asyncio

        self.ran = True
        if self.on_start:
            await self.on_start()
        await asyncio.sleep(self.seconds)
        return ToolResult(status=ToolStatus.SUCCEEDED, summary="slow done")

    async def reconcile(self, action):
        return ReconcileResult(status="unsupported")


def slow_setup(tmp_path, spec, connector, *, limits=None):
    policy = WhitelistPolicy(roles={"main_tools@1": {spec.name}}, users={ACTOR.scope_id: {AgentRole.MAIN: UserGrant()}})
    return Setup(tmp_path, policy=policy, connectors=[connector], limits=limits)


async def test_cancel_after_registration_stops_the_call(tmp_path):
    spec = ToolSpec(name="slow.read", description="", read_only=True, side_effects=False)
    conn = _Slow(spec, seconds=0)
    s = await slow_setup(tmp_path, spec, conn).start()
    # A cancel that lands between registering and running: patch the
    # repository so the request arrives right after the action is registered.
    original = s.repos.actions.update

    async def update_then_cancel(action_id, status, **kw):
        out = await original(action_id, status, **kw)
        if status is ActionStatus.RUNNING:
            await s.repos.tasks.request_cancel("s1", s.task.id, "user", T0)
        return out

    s.repos.actions.update = update_then_cancel
    outcome = await s.call("slow.read")
    assert outcome.status is ToolStatus.DENIED and outcome.error.code is ErrorCode.CANCELLED
    assert not conn.ran
    (action,) = await s.actions()
    assert action.status is ActionStatus.FAILED
    assert await s.repos.usage.open_reservations(s.task.id) == []
    assert (await s.usage()).tool_calls == 0


async def test_read_only_call_past_the_deadline_is_failed(tmp_path):
    import datetime as dt

    spec = ToolSpec(name="slow.read", description="", read_only=True, side_effects=False)
    s = await slow_setup(tmp_path, spec, _Slow(spec, seconds=5), limits=Limits(timeout_sec=60)).start()
    # Leave 0.05 s before the deadline.
    s.clock.now = s.task.deadline - dt.timedelta(seconds=0.05)
    outcome = await s.call("slow.read")
    assert outcome.status is ToolStatus.FAILED and outcome.error.code is ErrorCode.DEADLINE_EXCEEDED
    assert await s.repos.usage.open_reservations(s.task.id) == []


async def test_writing_call_past_the_deadline_is_unknown_and_keeps_its_reservation(tmp_path):
    import datetime as dt

    spec = ToolSpec(name="slow.write", description="", read_only=False, side_effects=False)
    s = await slow_setup(tmp_path, spec, _Slow(spec, seconds=5), limits=Limits(timeout_sec=60)).start()
    s.clock.now = s.task.deadline - dt.timedelta(seconds=0.05)
    outcome = await s.call("slow.write")
    assert outcome.status is ToolStatus.UNKNOWN and outcome.error.code is ErrorCode.DEADLINE_EXCEEDED
    (action,) = await s.actions()
    assert action.status is ActionStatus.UNKNOWN
    assert len(await s.repos.usage.open_reservations(s.task.id)) == 1
