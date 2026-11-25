"""ToolGateway: every tool call passes through here (spec 2, 6.2).

The five steps of spec 6.2, in order:

1. Normalise arguments, compute ``request_hash``, look up an earlier action
   with the same ``logical_key``. Same key with other arguments is a conflict;
   an earlier success is reused without running the tool again. An earlier
   ``unknown`` outcome, or a call that changes things and never finished
   (``planned`` / ``running``, e.g. its worker died), is not run again: it
   needs reconciliation. A failed call, or an unfinished read-only one, is
   retried and charged again (review of #16).
2. Check the lease, cancellation, deadline, tool scope and grants. Tools
   that are visible outside or hard to undo need approval (none in P0).
3. Register the action and reserve one tool call in one step
   (``ActionRepository.register(..., reserve=...)``).
4. Call the connector. Credentials are resolved by reference, never passed
   in arguments. Settle real usage afterwards; an unknown outcome keeps its
   reservation.
5. Store the receipt and result, mark the action, and record a
   ``tool_action`` event. A side effect whose outcome is uncertain is marked
   ``unknown`` and not retried blindly.

Calls that are refused (steps 1-3) never reach a connector. Refusals before
registration return no ``action_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contracts import (
    ApprovalDecision,
    UsageKind,
    new_id,
    Action,
    ActionConflict,
    ActionRef,
    ActionStatus,
    Actor,
    ArtifactInput,
    AuthorizedCall,
    ErrorCode,
    Event,
    EventType,
    ExecutionContext,
    HarnessError,
    NotFound,
    ReconcileResult,
    StaleLease,
    Task,
    TaskError,
    TaskStatus,
    ToolCall,
    ToolOutcome,
    ToolResult,
    ToolSpec,
    ToolStatus,
    Usage,
    canonical_json,
    utcnow,
)
from ..interfaces import ArtifactStore, Connector
from ..storage.repositories import EventRepository, Repositories
from .policy import WhitelistPolicy

if TYPE_CHECKING:
    from .connectors.data import DataConnector

log = logging.getLogger(__name__)

TOOL_CALL = Usage(tool_calls=1)
# Tools whose arguments name a file the policy must check (P0: files.read).
PATH_ARGS = {"files.read": "path", "data.connect": "path", "data.profile": "path"}
# Tools that are handed the directories the user allows, so they can list them.
PATH_SCOPE_TOOLS = {"data.list"}

_ACTION_FOR_RESULT = {
    ToolStatus.SUCCEEDED: ActionStatus.SUCCEEDED,
    ToolStatus.FAILED: ActionStatus.FAILED,
    ToolStatus.UNKNOWN: ActionStatus.UNKNOWN,
}


def _refused(status: ToolStatus, code: ErrorCode, message: str, action_id: str | None = None) -> ToolOutcome:
    return ToolOutcome(action_id=action_id, status=status, error=TaskError(code=code, message=message))


def _not_retryable(action: Action) -> ToolOutcome | None:
    """Refuse to run an earlier call again when that could do it twice."""
    if action.status is ActionStatus.UNKNOWN:
        return _refused(
            ToolStatus.UNKNOWN,
            ErrorCode.TOOL_ERROR,
            "an earlier attempt of this call has an unknown outcome; it needs reconciliation",
            action.id,
        )
    if action.status in (ActionStatus.PLANNED, ActionStatus.RUNNING) and not action.read_only:
        return _refused(
            ToolStatus.UNKNOWN,
            ErrorCode.TOOL_ERROR,
            "an earlier attempt of this call did not finish, so whether it took effect is unknown; "
            "it needs reconciliation",
            action.id,
        )
    return None


def _schema_problem(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    """Validate arguments against the tool's input schema (spec 7 step 2)."""
    if not spec.input_schema:
        return None
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema ships with the repo
        return None
    try:
        jsonschema.validate(args, spec.input_schema)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path)
        return f"invalid arguments{' at ' + where if where else ''}: {exc.message}"
    return None


class ToolGateway:
    """``interfaces.ToolGateway`` for P0.

    ``profiles`` maps an agent profile ref (``main@1``) to its
    ``tool_policy_ref``; the gateway needs it to apply the role whitelist and
    cannot trust the model to say which profile it runs under.
    """

    def __init__(
        self,
        repos: Repositories,
        artifacts: ArtifactStore,
        policy: WhitelistPolicy,
        connectors: Iterable[Connector],
        *,
        profiles: dict[str, str],
        clock: Callable[[], datetime] = utcnow,
        delivery=None,
        data_sessions: DataConnector | None = None,
    ) -> None:
        self._repos = repos
        self._artifacts = artifacts
        self._policy = policy
        self._connectors = {c.spec.name: c for c in connectors}
        self._profiles = dict(profiles)
        self._clock = clock
        self._delivery = delivery
        self._data_sessions = data_sessions

    @property
    def specs(self) -> list[ToolSpec]:
        return [c.spec for c in self._connectors.values()]

    async def invoke(self, execution: ExecutionContext, call: ToolCall) -> ToolOutcome:
        if not call.logical_key:
            raise ValueError("ToolGateway needs call.logical_key; the Runner sets it from the step ID")
        lease = execution.lease
        task = await self._repos.tasks.get(lease.scope_id, lease.task_id)
        connector = self._connectors.get(call.tool)
        artifact = None
        # Cached content must obey current permissions, including subscription
        # revocation and (for incidental Main reads) freshness.
        if connector is not None and call.tool in {"artifacts.read", "background_results.read", "background_results.mention"}:
            problem = _schema_problem(connector.spec, call.args)
            if problem is not None:
                return _refused(ToolStatus.FAILED, ErrorCode.INVALID_REQUEST, problem)
            try:
                if call.tool == "artifacts.read":
                    artifact = await self._repos.artifacts.get(task.scope_id, call.args["artifact_id"])
                elif self._delivery is not None:
                    item = await self._repos.delivery.get(task.scope_id, call.args["id"])
                    if not await self._delivery.accessible(task.scope_id, item, explicit=call.args.get("explicit", False)):
                        return _refused(ToolStatus.DENIED, ErrorCode.FORBIDDEN, "background result is no longer accessible")
            except NotFound:
                return _refused(ToolStatus.FAILED, ErrorCode.NOT_FOUND, "result or artifact not found")
        if artifact is not None and not task.subscription_id:
            if artifact.task_id:
                source = await self._repos.tasks.get(task.scope_id, artifact.task_id)
                if source.subscription_id:
                    from ..tasks.proactive_controller import active, resources_allowed
                    sub = await self._repos.subscriptions.get(task.scope_id, source.subscription_id)
                    if (not active(sub, self._clock()) or not resources_allowed(sub, self._policy)
                        or not self._policy.check_path(Path(source.subject_ref).resolve(), scope_id=task.scope_id,
                                                       role=task.agent_role).allowed):
                        return _refused(ToolStatus.DENIED, ErrorCode.FORBIDDEN, "background artifact permission revoked")
        if task.subscription_id:
            from ..tasks.proactive_controller import active, resources_allowed
            sub = await self._repos.subscriptions.get(task.scope_id, task.subscription_id)
            if not active(sub, self._clock()) or not resources_allowed(sub, self._policy):
                return _refused(ToolStatus.DENIED, ErrorCode.FORBIDDEN, "subscription is inactive or its resources were revoked")
            permission = self._policy.check_tool(call.tool, scope_id=task.scope_id, role=task.agent_role,
                tool_policy_ref=self._profiles[task.agent_profile_ref], capability_ref=task.capability_ref)
            if not permission.allowed:
                return _refused(ToolStatus.DENIED, ErrorCode.FORBIDDEN, permission.reason)
            if call.tool in PATH_ARGS:
                path = Path(str(call.args.get(PATH_ARGS[call.tool], ""))).expanduser().resolve()
                if path not in {Path(p).expanduser().resolve() for p in sub.resources}:
                    return _refused(ToolStatus.DENIED, ErrorCode.FORBIDDEN, "resource is outside the subscription")
            if artifact is not None:
                if artifact.task_id not in {task.id, task.source_task_id} or artifact.task_id is None:
                    return _refused(ToolStatus.DENIED, ErrorCode.FORBIDDEN, "proactive cannot read Main or unrelated artifacts")

        # -- step 1: same key seen before? --------------------------------
        request_hash = call.request_hash()
        earlier = await self._repos.actions.get(task.id, call.logical_key)
        if earlier is not None:
            if earlier.request_hash != request_hash:
                raise ActionConflict(
                    f"action key {call.logical_key} was already used with other arguments",
                    action_id=earlier.id,
                )
            if earlier.status is ActionStatus.SUCCEEDED:
                return await self._reuse(task.scope_id, earlier)
            refused = _not_retryable(earlier)
            if refused is not None:
                return refused

        # -- step 2: may this call run at all? -----------------------------
        now = self._clock()
        if task.current_attempt_id != lease.attempt_id or task.lease_epoch != lease.lease_epoch:
            raise StaleLease(f"lease {lease.attempt_id} is no longer current", task_id=task.id)
        if task.status is TaskStatus.CANCELLING or task.cancel_requested_at is not None:
            return _refused(ToolStatus.DENIED, ErrorCode.CANCELLED, "the task is being cancelled")
        if task.deadline is not None and now >= task.deadline:
            return _refused(ToolStatus.DENIED, ErrorCode.DEADLINE_EXCEEDED, "the task deadline has passed")
        if connector is None:
            return _refused(ToolStatus.DENIED, ErrorCode.FORBIDDEN, f"unknown tool {call.tool!r}")
        tool_policy_ref = self._profiles.get(task.agent_profile_ref)
        if tool_policy_ref is None:
            return _refused(ToolStatus.DENIED, ErrorCode.FORBIDDEN, f"no tool policy for profile {task.agent_profile_ref}")
        decision = self._policy.check_tool(
            call.tool,
            scope_id=execution.actor.scope_id,
            role=execution.agent_role,
            tool_policy_ref=tool_policy_ref,
            capability_ref=task.capability_ref,
        )
        if not decision.allowed:
            return _refused(ToolStatus.DENIED, ErrorCode.FORBIDDEN, decision.reason)
        problem = _schema_problem(connector.spec, call.args)
        if problem is not None:
            return _refused(ToolStatus.FAILED, ErrorCode.INVALID_REQUEST, problem)
        args = dict(call.args)
        path_arg = PATH_ARGS.get(call.tool)
        if path_arg is not None:
            resolved = Path(str(args.get(path_arg, ""))).expanduser().resolve()
            # Files named in input_refs get no exemption: input_refs come from
            # whoever created the task, so they are checked against the same
            # user whitelist (review of #16: "file:/etc/passwd" was readable).
            path_decision = self._policy.check_path(
                resolved,
                scope_id=execution.actor.scope_id,
                role=execution.agent_role,
            )
            if not path_decision.allowed:
                return _refused(ToolStatus.DENIED, ErrorCode.FORBIDDEN, path_decision.reason)
            args[path_arg] = str(resolved)
            decision = path_decision if path_decision.grant_ref else decision
        # A tool that changes things outside the task waits for a human decision.
        # Without an approvals store there is nothing to decide against, so such
        # tools stay closed (spec 8: only connectors with a side-effect test may
        # write to outside systems).
        if connector.spec.side_effects and self._repos.approvals is None:
            return _refused(
                ToolStatus.NEEDS_APPROVAL,
                ErrorCode.FORBIDDEN,
                f"{call.tool} changes things outside the task and needs approval",
            )

        # -- step 3: register and reserve together -------------------------
        args_ref = await self._store_json(task.scope_id, task.id, f"tool_args/{call.logical_key}", args)
        action = Action(
            task_id=task.id,
            attempt_id=lease.attempt_id,
            logical_key=call.logical_key,
            tool=call.tool,
            request_hash=request_hash,
            args_ref=args_ref,
            read_only=connector.spec.read_only,
            grant_ref=decision.grant_ref,
            # Written in the waiting state so a crash between registration and
            # parking cannot leave a side effect looking like an unknown run.
            status=ActionStatus.NEEDS_APPROVAL if connector.spec.side_effects else ActionStatus.PLANNED,
            created_at=now,
            updated_at=now,
        )
        registration = await self._repos.actions.register(
            action, reserve=None if connector.spec.side_effects else TOOL_CALL
        )
        if registration.action is None:
            exceeded = registration.budget.exceeded if registration.budget else []
            return _refused(
                ToolStatus.DENIED,
                ErrorCode.BUDGET_EXHAUSTED,
                f"no tool budget left ({', '.join(exceeded) or 'limits'})",
            )
        action = registration.action
        if not registration.created:
            if action.status is ActionStatus.SUCCEEDED:
                # Another worker finished the same call between our lookup
                # and registration; reuse it.
                return await self._reuse(task.scope_id, action)
            refused = _not_retryable(action)
            if refused is not None:
                return refused
        # A side effect runs only with an approval bound to exactly these
        # arguments. Without one the action is parked in needs_approval and the
        # task waits; nothing is executed and no tool budget is spent.
        approval_ref: str | None = None
        if connector.spec.side_effects:
            verdict, detail = await self._valid_approval(action, now)
            if verdict == "rejected":
                # The human said no: fail the action so the model sees a refusal
                # instead of being asked again for the same thing.
                await self._repos.actions.update(action.id, ActionStatus.FAILED, now=self._clock())
                return _refused(
                    ToolStatus.DENIED,
                    ErrorCode.FORBIDDEN,
                    f"{call.tool} was rejected: {detail}",
                    action.id,
                )
            if verdict is None:
                if registration.created:
                    await self._repos.events.append(
                        Event(
                            scope_id=task.scope_id,
                            task_id=task.id,
                            attempt_id=lease.attempt_id,
                            type=EventType.APPROVAL_REQUESTED,
                            payload={"action_id": action.id, "tool": call.tool, "logical_key": call.logical_key},
                            occurred_at=now,
                            received_at=now,
                        )
                    )
                return _refused(
                    ToolStatus.NEEDS_APPROVAL,
                    ErrorCode.FORBIDDEN,
                    f"{call.tool} changes things outside the task; approve action {action.id} to run it",
                    action.id,
                )
            approval_ref = detail
        # The budget charge for this run of the call. A new action that may run
        # now was charged by register(); a retry, or a side effect that was just
        # approved, is charged here under its own call ID.
        charge_id = action.id if registration.created and registration.budget is not None else None
        if charge_id is None:
            charge_id = f"{action.id}.{'retry' if not registration.created else 'run'}-{new_id('r')}"
            budget = await self._repos.usage.reserve(
                task.id, lease.attempt_id, charge_id, UsageKind.TOOL, TOOL_CALL, now
            )
            if not budget.granted:
                return _refused(
                    ToolStatus.DENIED,
                    ErrorCode.BUDGET_EXHAUSTED,
                    f"no tool budget left ({', '.join(budget.exceeded) or 'limits'})",
                    action.id,
                )
        await self._repos.actions.update(
            action.id, ActionStatus.RUNNING, now=self._clock(), approval_ref=approval_ref
        )

        # -- step 4: run it -------------------------------------------------
        allowed_paths: list[str] = []
        if call.tool in PATH_SCOPE_TOOLS:
            grant = self._policy.user_grant(execution.actor.scope_id, execution.agent_role)
            allowed_paths = [str(Path(p).expanduser().resolve()) for p in (grant.paths if grant else ())]
        authorized = AuthorizedCall(
            allowed_paths=allowed_paths,
            action_id=action.id,
            task_id=task.id,
            attempt_id=lease.attempt_id,
            scope_id=task.scope_id,
            tool=call.tool,
            args=args,
            logical_key=call.logical_key,
            credential_ref=None,
            deadline=task.deadline,
        )
        # Spec 4.4: "ToolGateway 在实际调用前重查" -- a cancel request that
        # arrived after step 2 must still stop the call. Nothing has run, so
        # the reservation is released and the action marked failed.
        latest = await self._repos.tasks.get(lease.scope_id, lease.task_id)
        if latest.status is TaskStatus.CANCELLING or latest.cancel_requested_at is not None:
            now = self._clock()
            if charge_id is not None:
                await self._repos.usage.release(charge_id, now)
            await self._repos.actions.update(action.id, ActionStatus.FAILED, now=now)
            return _refused(ToolStatus.DENIED, ErrorCode.CANCELLED, "the task is being cancelled", action.id)

        timeout = None
        if task.deadline is not None:
            timeout = (task.deadline - self._clock()).total_seconds()

        async def execute() -> ToolResult:
            restored = await self._restore_data_session(task, execution, authorized, tool_policy_ref)
            if restored is not None:
                return restored
            return await connector.execute(authorized)

        try:
            if timeout is not None and timeout <= 0:
                raise asyncio.TimeoutError
            if timeout is None:
                result = await execute()
            else:
                result = await asyncio.wait_for(execute(), timeout)
        except StaleLease:
            raise
        except asyncio.TimeoutError:
            # The call may still have done its work (spec 4.4: 接口超时也不代表"没有执行").
            message = f"{call.tool} did not finish before the task deadline"
            log.warning("connector %s timed out on action %s", call.tool, action.id)
            result = ToolResult(
                status=ToolStatus.FAILED if connector.spec.read_only else ToolStatus.UNKNOWN,
                error=TaskError(code=ErrorCode.DEADLINE_EXCEEDED, message=message),
            )
        except Exception as exc:  # noqa: BLE001 - connector bug or unexpected error
            log.exception("connector %s raised on action %s", call.tool, action.id)
            if connector.spec.read_only:
                # Nothing changed outside, so the call surely had no effect.
                result = ToolResult(
                    status=ToolStatus.FAILED,
                    error=TaskError(code=ErrorCode.TOOL_ERROR, message=f"{type(exc).__name__}: {exc}"),
                )
            else:
                result = ToolResult(
                    status=ToolStatus.UNKNOWN,
                    error=TaskError(code=ErrorCode.TOOL_ERROR, message=f"{type(exc).__name__}: {exc}"),
                )

        # -- step 5: settle, record, answer ---------------------------------
        now = self._clock()
        if charge_id is not None:
            if result.status is ToolStatus.UNKNOWN:
                pass  # keep the reservation until reconciled (spec 6.2 step 4)
            else:
                await self._repos.usage.settle(charge_id, TOOL_CALL, now)
        result_ref = await self._store_json(
            task.scope_id, task.id, f"tool_result/{call.logical_key}", result.model_dump(mode="json")
        )
        await self._repos.actions.update(
            action.id,
            _ACTION_FOR_RESULT[result.status],
            result_ref=result_ref,
            receipt_ref=result.receipt_ref,
            now=now,
        )
        await self._repos.events.append(
            Event(
                scope_id=task.scope_id,
                task_id=task.id,
                attempt_id=lease.attempt_id,
                type=EventType.TOOL_ACTION,
                payload={
                    "action_id": action.id,
                    "tool": call.tool,
                    "logical_key": call.logical_key,
                    "status": str(result.status),
                    "read_only": connector.spec.read_only,
                    "args_ref": args_ref,
                    "result_ref": result_ref,
                },
                occurred_at=now,
                received_at=now,
            )
        )
        return ToolOutcome(
            action_id=action.id,
            status=result.status,
            summary=result.summary,
            artifact_refs=list(result.artifact_refs),
            receipt_ref=result.receipt_ref,
            error=result.error,
        )

    async def _restore_data_session(
        self, task: Task, execution: ExecutionContext, call: AuthorizedCall, tool_policy_ref: str,
    ) -> ToolResult | None:
        data = self._data_sessions
        lease = execution.lease
        if (data is None or call.tool not in {"data.connect", "data.query"}
                or lease.lease_epoch <= 1 or data.has_session(lease)):
            return None

        from .connectors.data import DataRegistration

        subscription_paths = None
        if task.subscription_id:
            from ..tasks.proactive_controller import active, resources_allowed

            subscription = await self._repos.subscriptions.get(task.scope_id, task.subscription_id)
            if not active(subscription, self._clock()) or not resources_allowed(subscription, self._policy):
                return ToolResult(status=ToolStatus.FAILED, error=TaskError(
                    code=ErrorCode.FORBIDDEN, message="subscription is inactive or its resources were revoked",
                ))
            snapshot = {Path(ref).expanduser().resolve() for ref in task.input_refs}
            current = {Path(ref).expanduser().resolve() for ref in subscription.resources}
            subscription_paths = snapshot & current
        registrations = []
        for action in await self._repos.actions.list_for_task(task.id):
            if (action.task_id != task.id or action.tool != "data.connect"
                    or not action.read_only or action.status is not ActionStatus.SUCCEEDED):
                continue
            try:
                if not action.args_ref or not action.result_ref:
                    raise ValueError("missing connection record")
                args = await self._read_json(task.scope_id, action.args_ref)
                if not isinstance(args["path"], str) or not Path(args["path"]).is_absolute():
                    raise ValueError("invalid connection path")
                path = Path(args["path"]).resolve()
                if subscription_paths is not None and path not in subscription_paths:
                    continue
                result = ToolResult.model_validate(await self._read_json(task.scope_id, action.result_ref))
                recorded = result.data or {}
                name, tables = recorded.get("table"), recorded.get("tables")
                if result.status is not ToolStatus.SUCCEEDED or not isinstance(name, str) or not name:
                    raise ValueError("invalid connection record")
                if tables is not None and (
                    not isinstance(tables, dict)
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in tables.items())
                ):
                    raise ValueError("invalid table names")
            except (HarnessError, KeyError, TypeError, ValueError):
                return ToolResult(status=ToolStatus.FAILED, error=TaskError(
                    code=ErrorCode.TOOL_ERROR, message="could not restore a previously recorded data connection",
                ))
            tool_allowed = self._policy.check_tool(
                action.tool, scope_id=task.scope_id, role=task.agent_role,
                tool_policy_ref=tool_policy_ref, capability_ref=task.capability_ref,
            ).allowed
            path_allowed = self._policy.check_path(path, scope_id=task.scope_id, role=task.agent_role).allowed
            if not tool_allowed or not path_allowed:
                return ToolResult(status=ToolStatus.FAILED, error=TaskError(
                    code=ErrorCode.FORBIDDEN, message="a previously connected source is no longer authorized",
                ))
            registrations.append(DataRegistration(path, name, tables))

        if not registrations:
            return None
        latest = await self._repos.tasks.get(lease.scope_id, lease.task_id)
        if latest.current_attempt_id != lease.attempt_id or latest.lease_epoch != lease.lease_epoch:
            raise StaleLease(f"lease {lease.attempt_id} is no longer current", task_id=task.id)
        if latest.cancel_requested_at is not None or latest.status is TaskStatus.CANCELLING:
            return ToolResult(status=ToolStatus.FAILED, error=TaskError(
                code=ErrorCode.CANCELLED, message="the task is being cancelled",
            ))
        return await data.restore(lease, call, registrations)

    async def _valid_approval(self, action: Action, now: datetime) -> tuple[str | None, str]:
        """("approved", ref) | ("rejected", reason) | (None, "") for these arguments."""
        found = await self._repos.approvals.get(action.id, action.request_hash)  # type: ignore[union-attr]
        if found is None:
            return None, ""
        if found.expires_at is not None and found.expires_at <= now:
            return None, ""
        if found.decision is ApprovalDecision.REJECT:
            return "rejected", found.reason or "no reason given"
        if found.decision is ApprovalDecision.APPROVE:
            return "approved", f"{action.id}@{action.request_hash[:12]}"
        return None, ""

    async def _reuse(self, scope_id: str, action: Action) -> ToolOutcome:
        """An earlier success of the same call: answer from its stored result."""
        summary = "(result of an earlier identical call)"
        refs: list[str] = []
        if action.result_ref:
            try:
                stored = await self._read_json(scope_id, action.result_ref)
                summary = stored.get("summary") or summary
                refs = list(stored.get("artifact_refs") or [])
            except (NotFound, HarnessError, ValueError):
                pass
        return ToolOutcome(
            action_id=action.id,
            status=ToolStatus.SUCCEEDED,
            summary=summary,
            artifact_refs=refs,
            receipt_ref=action.receipt_ref,
        )

    async def _store_json(self, scope_id: str, task_id: str, name: str, value: Any) -> str:
        # Trajectory content: task_id is left empty so it is not listed among
        # the task's work artifacts (same convention as the Runner).
        ref = await self._artifacts.put(
            ArtifactInput(
                scope_id=scope_id,
                task_id=None,
                name=f"{task_id}/{name}",
                media_type="application/json",
                content=canonical_json(value).encode("utf-8"),
            )
        )
        return ref.id

    async def _read_json(self, scope_id: str, ref: str) -> dict[str, Any]:
        content = await self._artifacts.read(ref, Actor(actor_id="harness", scope_id=scope_id))
        return json.loads(content.content.decode("utf-8"))

    async def reconcile(self, task: Task, action: Action, expected_attempt: int) -> ActionStatus | None:
        reference = ActionRef(action_id=action.id, **action.model_dump(include={
            "task_id", "logical_key", "tool", "request_hash", "receipt_ref",
        }))
        details = {}
        if action.tool not in self._connectors:
            result = ReconcileResult(status="unsupported")
            details["reason"] = "connector_missing"
        else:
            try:
                result = await self._connectors[action.tool].reconcile(reference)
            except (HarnessError, TimeoutError, OSError) as exc:
                result = ReconcileResult(status="unknown")
                details["error"] = {"type": type(exc).__name__, "message": str(exc)}
        status = action.status if result.status == "unsupported" else ActionStatus(result.status)
        return await self._record_reconciliation(task, action, result, status, None, expected_attempt, details)

    async def resolve(self, task: Task, action: Action, happened: bool, actor: Actor, expected_attempt: int) -> ActionStatus | None:
        receipt = await self._store_json(task.scope_id, task.id, f"resolution/{action.id}", {
            "outcome": "happened" if happened else "not_happened", "actor": actor.actor_id,
        })
        result = ReconcileResult(status="succeeded" if happened else "failed", receipt_ref=receipt)
        return await self._record_reconciliation(
            task, action, result, ActionStatus(result.status), actor.actor_id, expected_attempt, {},
        )

    async def _record_reconciliation(
        self, task: Task, action: Action, result: ReconcileResult, status: ActionStatus, actor: str | None,
        expected_attempt: int, details: dict[str, Any],
    ) -> ActionStatus | None:
        now = self._clock()
        result_ref = None
        if status in (ActionStatus.SUCCEEDED, ActionStatus.FAILED):
            result_ref = await self._store_json(task.scope_id, task.id, f"tool_result/{action.logical_key}", {
                "status": status.value, "summary": result.detail, "receipt_ref": result.receipt_ref,
            })
        event = Event(
            scope_id=task.scope_id, task_id=task.id, attempt_id=action.attempt_id,
            type=EventType.TOOL_RECONCILE, occurred_at=now, received_at=now,
            payload={"action_id": action.id, "status": result.status, "actor": actor, **details},
        )
        decided = await self._repos.actions.decide(
            task.scope_id, task.id, action.id, status, result_ref=result_ref, receipt_ref=result.receipt_ref,
            usage=TOOL_CALL, event=event, now=now, expected_attempt=expected_attempt,
        )
        return decided.status if decided is not None else None


async def reconciliation_history(events: EventRepository, task: Task) -> list[Event]:
    history, cursor = [], 0
    while True:
        page = await events.list(task.scope_id, task_id=task.id, after_seq=cursor, limit=500)
        if not page:
            return history
        history.extend(event for event in page if event.type is EventType.TOOL_RECONCILE)
        cursor = page[-1].seq
