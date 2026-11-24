from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from harness.contracts import (
    TASK_TRANSITIONS,
    Acceptance,
    Approval,
    ApprovalDecision,
    Artifact,
    ArtifactContent,
    ArtifactRef,
    Actor,
    AgentProfile,
    AgentRole,
    AssistantStep,
    CandidateResult,
    CheckResult,
    CheckSpec,
    DeliveryOutcome,
    DeliveryStatus,
    ErrorCode,
    ErrorResponse,
    EventType,
    IdempotencyConflict,
    InvalidTransition,
    Grant,
    Limits,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    NoticeOutcome,
    NoticeRequest,
    NoticeReservation,
    OutboxItem,
    OutboxStatus,
    RemainingBudget,
    RunLane,
    StepKind,
    AdmissionDecision,
    ProactivityLevel,
    Subscription,
    SubscriptionStatus,
    Task,
    TaskError,
    TaskOrigin,
    TaskOutcome,
    TaskRef,
    TaskRequest,
    TaskStatus,
    ToolCall,
    ToolResult,
    ToolSpec,
    ToolStatus,
    TriggerType,
    Usage,
    VerificationResult,
    VerificationStatus,
    WaitCondition,
    assert_transition,
    can_transition,
    canonical_hash,
    split_versioned_ref,
)

S = TaskStatus


def main_origin(**kw):
    data = dict(
        agent_role=AgentRole.MAIN,
        agent_profile_ref="main@1",
        run_lane=RunLane.FOREGROUND,
        trigger_type=TriggerType.USER,
    )
    data.update(kw)
    return TaskOrigin(**data)


def request(**kw):
    data = dict(request_key="k1", goal="draft", capability_ref="document_draft@1")
    data.update(kw)
    return TaskRequest(**data)


# -- state machine ------------------------------------------------------------


@pytest.mark.parametrize(
    "current,target",
    [
        (S.QUEUED, S.RUNNING),
        (S.QUEUED, S.CANCELLED),
        (S.RUNNING, S.SUCCEEDED),
        (S.RUNNING, S.QUEUED),
        (S.RUNNING, S.WAITING),
        (S.RUNNING, S.FAILED),
        (S.RUNNING, S.CANCELLING),
        (S.WAITING, S.QUEUED),
        (S.WAITING, S.CANCELLED),
        (S.CANCELLING, S.CANCELLED),
    ],
)
def test_spec_transitions_allowed(current, target):
    assert can_transition(current, target)
    assert_transition(current, target)


def test_every_other_transition_is_rejected():
    allowed = {(a, b) for a, targets in TASK_TRANSITIONS.items() for b in targets}
    assert len(allowed) == 10
    for a in TaskStatus:
        for b in TaskStatus:
            if (a, b) not in allowed:
                assert not can_transition(a, b), (a, b)
    with pytest.raises(InvalidTransition):
        assert_transition(S.SUCCEEDED, S.RUNNING)
    with pytest.raises(InvalidTransition):
        assert_transition(S.WAITING, S.RUNNING)


def test_terminal_states_have_no_exits():
    for status in (S.SUCCEEDED, S.FAILED, S.CANCELLED):
        assert status.terminal
        assert TASK_TRANSITIONS[status] == frozenset()
    assert not S.CANCELLING.terminal


# -- limits and budget ----------------------------------------------------------


def test_limits_defaults_match_spec_example():
    assert Limits().model_dump() == {
        "timeout_sec": 1800,
        "max_model_calls": 20,
        "max_tool_calls": 50,
        "max_total_tokens": 100000,
    }


@pytest.mark.parametrize("field", ["timeout_sec", "max_model_calls", "max_tool_calls", "max_total_tokens"])
def test_limits_must_be_positive(field):
    with pytest.raises(ValidationError):
        Limits(**{field: 0})


def test_limits_reject_unknown_fields():
    with pytest.raises(ValidationError):
        Limits(max_steps=3)


def test_usage_add_and_exceeded():
    total = Usage(model_calls=1, input_tokens=10) + Usage(model_calls=2, tool_calls=1, output_tokens=5)
    assert total.model_calls == 3 and total.tool_calls == 1 and total.total_tokens == 15
    limits = Limits(max_model_calls=2, max_tool_calls=5, max_total_tokens=15)
    assert total.exceeded(limits) == ["max_model_calls"]
    assert Usage(input_tokens=16).exceeded(limits) == ["max_total_tokens"]


def test_remaining_budget_takes_tightest_ratio():
    limits = Limits(timeout_sec=100, max_model_calls=10, max_tool_calls=50, max_total_tokens=1000)
    usage = Usage(model_calls=8, tool_calls=5, input_tokens=100)
    remaining = RemainingBudget.compute(limits, usage, elapsed_sec=10)
    assert remaining.model_calls == 2
    assert remaining.tool_calls == 45
    assert remaining.total_tokens == 900
    assert remaining.seconds == 90
    assert remaining.fraction == pytest.approx(0.2)


def test_remaining_budget_never_negative():
    limits = Limits(max_model_calls=2)
    remaining = RemainingBudget.compute(limits, Usage(model_calls=5), elapsed_sec=99999)
    assert remaining.model_calls == 0 and remaining.seconds == 0 and remaining.fraction == 0.0


# -- requests and tasks ---------------------------------------------------------


def test_request_hash_ignores_key_and_is_stable():
    a = request(request_key="a", input_refs=["artifact:1"])
    b = request(request_key="b", input_refs=["artifact:1"])
    assert a.request_hash() == b.request_hash()
    assert a.request_hash().startswith("sha256:")
    assert request(goal="other").request_hash() != a.request_hash()


def test_canonical_hash_ignores_key_order():
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})


def test_request_cannot_choose_role_or_lane():
    with pytest.raises(ValidationError):
        request(agent_role="proactive")
    with pytest.raises(ValidationError):
        request(run_lane="background")


def test_request_needs_versioned_capability():
    with pytest.raises(ValidationError):
        request(capability_ref="document_draft")
    assert split_versioned_ref("document_draft@1") == ("document_draft", "1")


def test_acceptance_check_keeps_parameters():
    acceptance = Acceptance(
        required_artifacts=["draft"],
        checks=[{"id": "artifact_readable"}, {"id": "required_sections", "sections": ["目标", "方案"]}],
    )
    assert acceptance.checks[1].model_dump() == {"id": "required_sections", "sections": ["目标", "方案"]}
    with pytest.raises(ValidationError):
        CheckSpec(sections=["x"])


def test_task_new_copies_request_and_origin():
    actor = Actor(actor_id="u1", scope_id="s1")
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    task = Task.new(request(limits=Limits(max_model_calls=5)), actor, main_origin(priority=3), now=now)
    assert task.id.startswith("task_")
    assert task.scope_id == "s1" and task.actor_id == "u1"
    assert task.status is S.QUEUED and task.version == 1 and task.lease_epoch == 0
    assert task.agent_role is AgentRole.MAIN and task.run_lane is RunLane.FOREGROUND
    assert task.limits.max_model_calls == 5 and task.priority == 3
    assert task.request_hash == request().model_copy(update={"limits": Limits(max_model_calls=5)}).request_hash()
    assert task.created_at == now


def test_proactive_origin_rules():
    good = TaskOrigin(
        agent_role=AgentRole.PROACTIVE,
        agent_profile_ref="proactive@1",
        run_lane=RunLane.BACKGROUND,
        trigger_type=TriggerType.EVENT,
        subscription_id="sub_1",
    )
    assert good.subscription_id == "sub_1"
    with pytest.raises(ValidationError, match="background"):
        TaskOrigin(**{**good.model_dump(), "run_lane": "foreground"})
    with pytest.raises(ValidationError, match="subscription_id"):
        TaskOrigin(**{**good.model_dump(), "subscription_id": None})
    with pytest.raises(ValidationError, match="does not match"):
        TaskOrigin(**{**good.model_dump(), "agent_profile_ref": "main@1"})


def test_main_may_use_background_lane():
    assert main_origin(run_lane=RunLane.BACKGROUND).run_lane is RunLane.BACKGROUND


def test_task_wait_condition_matches_status():
    task = Task.new(request(), Actor(actor_id="u", scope_id="s"), main_origin())
    with pytest.raises(ValidationError):
        Task(**{**task.model_dump(), "status": "waiting"})
    waiting = Task(**{**task.model_dump(), "status": "waiting", "wait_condition": "input"})
    assert waiting.wait_condition is WaitCondition.INPUT


# -- outcomes, steps, verification ---------------------------------------------


def test_succeeded_outcome_needs_result_and_verification():
    with pytest.raises(ValidationError):
        TaskOutcome(status=S.SUCCEEDED, result_ref="art_1")
    ok = TaskOutcome(status=S.SUCCEEDED, result_ref="art_1", verification_ref="ver_1")
    assert ok.status is S.SUCCEEDED


def test_failed_outcome_keeps_artifacts_and_needs_error():
    with pytest.raises(ValidationError):
        TaskOutcome(status=S.FAILED)
    failed = TaskOutcome(
        status=S.FAILED,
        error=TaskError(code=ErrorCode.BUDGET_EXHAUSTED, message="max_model_calls"),
        artifact_refs=["art_partial"],
    )
    assert failed.artifact_refs == ["art_partial"]


def test_outcome_cannot_end_running_or_cancelling():
    for status in (S.RUNNING, S.CANCELLING):
        with pytest.raises(ValidationError):
            TaskOutcome(status=status)
    with pytest.raises(ValidationError):
        TaskOutcome(status=S.WAITING)
    assert TaskOutcome(status=S.WAITING, wait_condition="approval").wait_condition is WaitCondition.APPROVAL


def test_assistant_step_shapes():
    call = ToolCall(tool="files.read", args={"path": "a.md"})
    assert AssistantStep(kind=StepKind.TOOL_CALLS, tool_calls=[call], model="fake").tool_calls[0].logical_key is None
    with pytest.raises(ValidationError):
        AssistantStep(kind=StepKind.TOOL_CALLS, model="fake")
    with pytest.raises(ValidationError):
        AssistantStep(kind=StepKind.CANDIDATE_RESULT, tool_calls=[call], model="fake")
    candidate = CandidateResult(summary="done", artifact_refs={"draft": "art_1"})
    assert AssistantStep(kind=StepKind.CANDIDATE_RESULT, candidate=candidate, model="fake").candidate is candidate


def test_tool_call_hash_depends_on_args_only():
    a = ToolCall(tool="files.read", args={"path": "a"}, logical_key="k1")
    b = ToolCall(tool="files.read", args={"path": "a"}, logical_key="k2")
    assert a.request_hash() == b.request_hash()
    assert a.request_hash() != ToolCall(tool="files.read", args={"path": "b"}).request_hash()


def test_tool_spec_read_only_has_no_side_effects():
    with pytest.raises(ValidationError):
        ToolSpec(name="x", description="", read_only=True, side_effects=True)


def test_passed_verification_needs_all_checks_passed():
    with pytest.raises(ValidationError):
        VerificationResult(
            task_id="t",
            result_ref="r",
            checker_version="1",
            status=VerificationStatus.PASSED,
            checks=[CheckResult(id="a", status=VerificationStatus.INCONCLUSIVE)],
        )


def test_proactive_profile_is_background():
    with pytest.raises(ValidationError):
        AgentProfile(
            role=AgentRole.PROACTIVE,
            version="1",
            instructions_ref="prompts/p.md",
            tool_policy_ref="policy/p",
            default_lane=RunLane.FOREGROUND,
            default_delivery_mode="silent",
        )
    profile = AgentProfile(
        role=AgentRole.MAIN,
        version="1",
        instructions_ref="prompts/main.md",
        tool_policy_ref="policy/main",
        default_lane=RunLane.FOREGROUND,
        default_delivery_mode="reply",
    )
    assert profile.ref == "main@1"


def test_error_response_shape():
    error = IdempotencyConflict("request_key reused", task_id="t1")
    body = ErrorResponse.from_error(error, request_id="req_1").model_dump(mode="json")
    assert body == {
        "error_code": "idempotency_conflict",
        "message": "request_key reused",
        "retryable": False,
        "request_id": "req_1",
    }


# -- additions for module interfaces ---------------------------------------------


def test_remaining_budget_for_task_counts_from_deadline():
    task = Task.new(
        request(limits=Limits(timeout_sec=100, max_model_calls=10)),
        Actor(actor_id="u", scope_id="s"),
        main_origin(),
    )
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    assert RemainingBudget.for_task(task, now).seconds == 100
    started = task.model_copy(update={"deadline": now + timedelta(seconds=100), "usage": Usage(model_calls=5)})
    later = RemainingBudget.for_task(started, now + timedelta(seconds=30))
    assert later.seconds == 70 and later.model_calls == 5
    assert later.fraction == pytest.approx(0.5)
    assert RemainingBudget.for_task(started, now + timedelta(seconds=500)).seconds == 0


def test_connector_result_statuses():
    assert ToolResult(status=ToolStatus.UNKNOWN).status is ToolStatus.UNKNOWN
    for status in (ToolStatus.DENIED, ToolStatus.NEEDS_APPROVAL):
        with pytest.raises(ValidationError):
            ToolResult(status=status)
    with pytest.raises(ValidationError):
        ToolResult(status=ToolStatus.FAILED)


def test_artifact_ref_and_content():
    art = Artifact(
        scope_id="s", name="draft", uri="file:///x", media_type="text/markdown", content_hash="sha256:x", size_bytes=5
    )
    ref = ArtifactRef.of(art)
    assert ref.id == art.id and ref.name == "draft"
    assert ArtifactContent(ref=ref, content=b"hello").complete
    assert not ArtifactContent(ref=ref, content=b"he").complete


def test_task_ref_of():
    task = Task.new(request(), Actor(actor_id="u", scope_id="s"), main_origin())
    assert TaskRef.of(task).model_dump(mode="json") == {
        "task_id": task.id,
        "agent_role": "main",
        "run_lane": "foreground",
        "status": "queued",
    }


# -- subscriptions, approvals and delivery (P1/P2 types) -----------------------


def subscription(**kw):
    data = dict(
        scope_id="s1",
        owner_id="u1",
        goal="watch refunds",
        capability_ref="document_draft@1",
        schedule="*/30 * * * *",
    )
    data.update(kw)
    return Subscription(**data)


def test_subscription_needs_a_trigger():
    with pytest.raises(ValidationError):
        subscription(schedule=None)
    assert subscription().status is SubscriptionStatus.ACTIVE


def test_subscription_defaults_are_safe():
    sub = subscription()
    assert sub.id.startswith("sub_")
    assert sub.version == 1
    assert sub.delivery_mode == "digest"
    assert sub.limits.max_tool_calls > 0
    assert sub.proactivity is ProactivityLevel.NORMAL
    assert sub.daily_budget.total_tokens == 0
    assert sub.resources == []
    assert sub.result_ttl_seconds > 0
    # A filter alone is a valid trigger too.
    assert subscription(schedule=None, event_filter={"type": "data_updated"}).schedule is None


def test_subscription_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        subscription(scope="s1")


def test_subscription_watch_fields_optional():
    old_json = {
        "scope_id": "s1", "owner_id": "u1", "goal": "watch refunds",
        "capability_ref": "document_draft@1", "schedule": "*/30 * * * *",
    }
    old = Subscription.model_validate(old_json)
    assert old.baseline_ref is None and old.watch is None

    watch = {"query_index": 0, "column": "refunds", "min_relative_change": 0.1}
    configured = Subscription.model_validate({**old_json, "baseline_ref": "memory:mem_1@2", "watch": watch})
    assert configured.baseline_ref == "memory:mem_1@2" and configured.watch == watch


def test_admission_decision_is_the_per_subscription_record():
    # AdmissionDecision already existed; it doubles as the (subscription, event)
    # dedup record, so there is no separate SubscriptionEvent type.
    first = AdmissionDecision(
        subscription_id="sub_1", event_id="evt_1", decision="admit",
        reason="changed", policy_version="p1", task_id="task_1",
    )
    second = AdmissionDecision(
        subscription_id="sub_2", event_id="evt_1", decision="skip",
        reason="deduped", policy_version="p1",
    )
    assert first.task_id and second.task_id is None
    assert first.decided_at.tzinfo is not None


def test_admission_label_source_optional():
    old_json = {
        "subscription_id": "sub_1", "event_id": "evt_1", "decision": "skip",
        "reason": "deduped", "policy_version": "p1", "backfill_label": "useful",
    }
    assert AdmissionDecision.model_validate(old_json).label_source is None
    for source in ("explicit", "behavior", "manual"):
        assert AdmissionDecision.model_validate({**old_json, "label_source": source}).label_source == source
    with pytest.raises(ValidationError, match="label_source"):
        AdmissionDecision.model_validate({**old_json, "label_source": "inferred"})


def test_recompute_recorded_event_type():
    assert EventType("recompute_recorded") is EventType.RECOMPUTE_RECORDED


def test_approval_binds_action_and_request_hash():
    approval = Approval(action_id="act_1", request_hash="h1", approver="u1", decision=ApprovalDecision.APPROVE)
    assert approval.decision is ApprovalDecision.APPROVE
    with pytest.raises(ValidationError):
        Approval(action_id="act_1", request_hash="h1", approver="u1", decision="yes")


def test_grant_expiry_and_revocation_make_it_inactive():
    live = Grant(key="send_email:ops@company.com", subject="u1", resource="ops@company.com", action="send_email")
    assert live.active
    expired = live.model_copy(update={"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)})
    assert not expired.active
    revoked = live.model_copy(update={"revoked_at": datetime.now(timezone.utc)})
    assert not revoked.active


def test_notice_request_needs_a_dedup_key():
    with pytest.raises(ValidationError):
        NoticeRequest(scope_id="s1", recipient="u1", result_ref="a1", channel="feishu", dedup_key="")
    request = NoticeRequest(scope_id="s1", recipient="u1", result_ref="a1", channel="feishu", dedup_key="d1")
    assert request.mode == "notify"


def test_notice_reservation_can_be_suppressed():
    assert NoticeOutcome.ALREADY_RESERVED.value == "already_reserved"
    reservation = NoticeReservation(outcome=NoticeOutcome.SUPPRESSED, dedup_key="d1", reason="quiet hours")
    assert reservation.notice_id is None
    assert reservation.reason


def test_outbox_item_starts_pending():
    item = OutboxItem(scope_id="s1", recipient="u1", channel="feishu", content_ref="a1", dedup_key="d1")
    assert item.status is OutboxStatus.PENDING
    assert item.attempts == 0
    with pytest.raises(ValidationError):
        OutboxItem(
            scope_id="s1", recipient="u1", channel="feishu", content_ref="a1", dedup_key="d1", attempts=-1
        )


def test_delivered_outcome_needs_a_receipt():
    with pytest.raises(ValidationError):
        DeliveryOutcome(status=DeliveryStatus.DELIVERED)
    assert DeliveryOutcome(status=DeliveryStatus.DELIVERED, receipt_ref="rcpt_1").receipt_ref
    # An unknown send keeps its item for reconciliation without a receipt.
    assert DeliveryOutcome(status=DeliveryStatus.UNKNOWN).receipt_ref is None


def test_memory_record_is_candidate_until_confirmed():
    record = MemoryRecord(scope_id="s1", kind=MemoryKind.FACT, content_ref="a1", source_ref="msg_1")
    assert record.status is MemoryStatus.CANDIDATE
    confirmed = record.model_copy(update={"status": MemoryStatus.CONFIRMED})
    superseding = record.model_copy(update={"supersedes_id": confirmed.id})
    assert superseding.supersedes_id == confirmed.id


@pytest.fixture
def old_memory_json():
    return {
        "id": "mem_2", "scope_id": "s1", "kind": "result", "status": "confirmed",
        "content_ref": "art_2", "source_ref": "task:2", "version": 2,
        "supersedes_id": "mem_1", "expires_at": None,
        "created_at": "2026-11-20T09:00:00Z", "updated_at": "2026-11-21T09:00:00Z",
    }


def test_memory_kinds():
    assert MemoryKind("analysis") is MemoryKind.ANALYSIS
    assert MemoryKind("episode") is MemoryKind.EPISODE


def test_memory_record_new_fields_default(old_memory_json):
    record = MemoryRecord.model_validate(old_memory_json)
    assert record.summary == "" and record.tags == []
    assert record.subject_ref is None and record.ended_at is None and record.end_reason is None

    tags = [f"tag{i}" for i in range(16)]
    fields = {"summary": "x" * 300, "tags": tags, "subject_ref": "orders.csv"}
    populated = MemoryRecord.model_validate({**old_memory_json, **fields})
    assert populated.model_dump(include=set(fields)) == fields
    with pytest.raises(ValidationError, match="summary"):
        MemoryRecord.model_validate({**old_memory_json, "summary": "x" * 301})
    with pytest.raises(ValidationError, match="tags"):
        MemoryRecord.model_validate({**old_memory_json, "tags": [*tags, "overflow"]})


def test_memory_record_end_fields_together(old_memory_json):
    ended_at = datetime(2026, 11, 22, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate({**old_memory_json, "ended_at": ended_at})
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate({**old_memory_json, "end_reason": "retracted"})
    for reason in ("superseded", "retracted", "rejected"):
        record = MemoryRecord.model_validate({**old_memory_json, "ended_at": ended_at, "end_reason": reason})
        assert record.ended_at == ended_at and record.end_reason == reason
    with pytest.raises(ValidationError, match="end_reason"):
        MemoryRecord.model_validate({**old_memory_json, "ended_at": ended_at, "end_reason": "expired"})


def test_memory_record_reads_old_json(old_memory_json):
    record = MemoryRecord.model_validate(old_memory_json)
    assert record.kind is MemoryKind.RESULT
    assert record.model_dump(mode="json", include=set(old_memory_json)) == old_memory_json
    assert record.ended_at is None


def test_new_types_survive_a_json_round_trip():
    sub = subscription()
    again = Subscription.model_validate_json(sub.model_dump_json())
    assert again == sub
    assert canonical_hash(sub) == canonical_hash(again)

