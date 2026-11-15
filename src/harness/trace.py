"""One task's timeline, for people: what happened, in order, and what it cost.

Everything here is read from records the harness already writes (events,
actions, verifications, the task and its attempts); building a trace calls no
model and writes nothing. With the in-memory store it covers the current
process only; once the PostgreSQL repositories land the same call works for
any past task.

Calls the gateway refused before registering an action (permission,
arguments, path) leave no action or event by design (spec 6.2 step 2). The
trace still shows them: the refusal is the tool message the model saw in its
next input, and only its status and error line are copied, never a tool
result's content. A refusal answered by no later model call (the attempt
ended right after it) is not shown.

    trace = await build_trace(repos, artifacts, scope_id, task_id)
    print(render_trace(trace))
"""

from __future__ import annotations

import json
from typing import Any

from .contracts import Actor, EventType
from .interfaces import ArtifactStore
from .storage.repositories import Repositories

_PAGE = 500


async def build_trace(
    repos: Repositories, artifacts: ArtifactStore | None, scope_id: str, task_id: str
) -> dict[str, Any]:
    task = await repos.tasks.get(scope_id, task_id)
    events = []
    after = 0
    while True:
        page = await repos.events.list(scope_id, task_id=task_id, after_seq=after, limit=_PAGE)
        events += page
        if len(page) < _PAGE:
            break
        after = page[-1].seq

    attempts: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.attempt_id and e.attempt_id not in attempts:
            try:
                a = await repos.tasks.get_attempt(scope_id, e.attempt_id)
            except Exception:  # noqa: BLE001 -- a trace must not fail on one missing row
                attempts[e.attempt_id] = {"id": e.attempt_id}
                continue
            attempts[e.attempt_id] = {
                "id": a.id,
                "worker_id": a.worker_id,
                "lease_epoch": a.lease_epoch,
                "started_at": a.started_at.isoformat(),
                "ended_at": a.ended_at.isoformat() if a.ended_at else None,
                "exit_reason": a.exit_reason,
                "agent_profile_version": a.agent_profile_version,
                "capability_version": a.capability_version,
                "model_version": a.model_version,
                "context_ref": a.context_ref,
            }

    reader = Actor(actor_id="trace", scope_id=scope_id)
    start = events[0].occurred_at if events else task.created_at
    steps: list[dict[str, Any]] = []
    model_calls = [e for e in events if e.type is EventType.MODEL_CALL]
    refused = await _refused_calls(artifacts, reader, model_calls) if artifacts is not None else {}

    for e in events:
        item: dict[str, Any] = {
            "seq": e.seq,
            "t": round((e.occurred_at - start).total_seconds(), 3),
            "type": str(e.type),
            "attempt_id": e.attempt_id,
            **_summary(e.type, e.payload),
        }
        steps.append(item)
        if e.type is EventType.MODEL_CALL:
            for r in refused.get(e.payload.get("call_id"), []):
                steps.append({"seq": e.seq, "t": item["t"], "type": "tool_refused", "attempt_id": e.attempt_id, **r})

    return {
        "task": {
            "id": task.id,
            "scope_id": task.scope_id,
            "goal": task.goal,
            "capability_ref": task.capability_ref,
            "agent_role": str(task.agent_role),
            "agent_profile_ref": task.agent_profile_ref,
            "run_lane": str(task.run_lane),
            "status": str(task.status),
            "result_ref": task.result_ref,
            "error": task.error.model_dump(mode="json") if task.error else None,
        },
        "limits": task.limits.model_dump(mode="json"),
        "usage": {**task.usage.model_dump(mode="json"), "total_tokens": task.usage.total_tokens},
        "attempts": list(attempts.values()),
        "steps": steps,
    }


def _summary(kind: EventType, p: dict[str, Any]) -> dict[str, Any]:
    if kind is EventType.TASK_CREATED:
        return {"status": p.get("status")}
    if kind is EventType.TASK_STATUS_CHANGED:
        out = {"from": p.get("from"), "to": p.get("to")}
        for key in ("worker_id", "error", "wait_condition", "reason"):
            if p.get(key) is not None:
                out[key] = p[key]
        return out
    if kind is EventType.CONTEXT_BUILT:
        sizes = p.get("sizes") or {}
        return {
            "call_id": p.get("call_id"),
            "wrap_up": p.get("wrap_up", False),
            "est_tokens": sizes.get("est_tokens"),
            "sections": {s["name"]: s.get("est_tokens") for s in sizes.get("sections", [])},
        }
    if kind is EventType.MODEL_CALL:
        usage = p.get("usage") or {}
        return {
            "call_id": p.get("call_id"),
            "model": p.get("model"),
            "step_kind": p.get("step_kind"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "latency_ms": p.get("latency_ms"),
            "input_ref": p.get("input_ref"),
            "output_ref": p.get("output_ref"),
        }
    if kind is EventType.TOOL_ACTION:
        return {
            k: p.get(k)
            for k in ("tool", "status", "logical_key", "read_only", "action_id", "args_ref", "result_ref")
        }
    if kind is EventType.VERIFICATION_RECORDED:
        return {k: p.get(k) for k in ("status", "checks", "unverified", "result_ref")}
    if kind is EventType.BUDGET_REMINDER:
        return {"tier": p.get("tier"), "remaining": p.get("remaining")}
    return {"payload": p}


async def _refused_calls(
    artifacts: ArtifactStore, reader: Actor, model_calls: list
) -> dict[str, list[dict[str, Any]]]:
    """Refusals the model was told about, keyed by the call that asked.

    The input of model call N+1 holds the tool messages answering call N.
    Refused calls have no action, so they are the tool messages whose status
    is ``denied`` or ``failed`` without a matching action. Only those two
    lines are kept.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for asked, answered in zip(model_calls, model_calls[1:]):
        ref = answered.payload.get("input_ref")
        if not ref:
            continue
        try:
            body = json.loads((await artifacts.read(ref, reader)).content)
        except Exception:  # noqa: BLE001
            continue
        messages = (body.get("context") or {}).get("messages") or []
        # Only the replies to the call just before: the tool messages after
        # the last assistant message.
        last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        for m in messages[last + 1:]:
            if m.get("role") != "tool" or not m.get("is_error"):
                continue
            lines = str(m.get("content", "")).splitlines()
            if any(line.startswith("action:") for line in lines):
                continue  # registered, so it already has a tool_action event
            if lines and lines[0].startswith("status: "):
                # Refused by the gateway: "status: denied" + "error (code): reason".
                status = lines[0].removeprefix("status: ").strip()
                error = next((line for line in lines[1:] if line.startswith("error")), "")
                error = error.removeprefix("error ").strip()
            else:
                # Refused by the Runner before the gateway (unknown tool, bad
                # arguments, submit_result mixed with other calls).
                status, error = "rejected", (lines[0] if lines else "")
            out.setdefault(asked.payload.get("call_id"), []).append(
                {"tool": m.get("name"), "status": status, "error": error[:300]}
            )
    return out


# -- text ---------------------------------------------------------------------------


def render_trace(trace: dict[str, Any]) -> str:
    t = trace["task"]
    u, lim = trace["usage"], trace["limits"]
    lines = [
        f"task {t['id']}  [{t['status']}]",
        f"  goal: {t['goal']}",
        f"  {t['agent_role']} / {t['run_lane']}  profile {t['agent_profile_ref']}  capability {t['capability_ref']}",
    ]
    for a in trace["attempts"]:
        lines.append(
            f"  attempt {a['id']}  worker {a.get('worker_id')}  epoch {a.get('lease_epoch')}"
            f"  model {a.get('model_version')}  exit {a.get('exit_reason')}"
        )
    lines.append("")
    for s in trace["steps"]:
        lines.append(f"{s['t']:>8.2f}s  {_line(s)}")
    lines.append("")
    lines.append(
        f"usage  model calls {u['model_calls']}/{lim['max_model_calls']}"
        f"  tool calls {u['tool_calls']}/{lim['max_tool_calls']}"
        f"  tokens {u['total_tokens']}/{lim['max_total_tokens']}"
        f" (in {u['input_tokens']}, out {u['output_tokens']})"
    )
    if t["result_ref"]:
        lines.append(f"result {t['result_ref']}")
    if t["error"]:
        lines.append(f"error  {t['error']['code']}: {t['error']['message']}")
    return "\n".join(lines)


def _line(s: dict[str, Any]) -> str:
    kind = s["type"]
    if kind == "task_created":
        return f"created            -> {s['status']}"
    if kind == "task_status_changed":
        extra = f"  ({s['worker_id']})" if s.get("worker_id") else ""
        if s.get("error"):
            err = s["error"]
            extra += f"  {err.get('code')}: {err.get('message')}" if isinstance(err, dict) else f"  {err}"
        return f"status             {s['from']} -> {s['to']}{extra}"
    if kind == "context_built":
        wrap = "  [wrap-up: only submit_result offered]" if s.get("wrap_up") else ""
        parts = ", ".join(f"{k} {v}" for k, v in (s.get("sections") or {}).items())
        return f"context            ~{s.get('est_tokens')} tok ({parts}){wrap}"
    if kind == "model_call":
        return (
            f"model {s.get('model')}  -> {s.get('step_kind')}  in {s.get('input_tokens')} / out {s.get('output_tokens')} tok"
            f"  {s.get('latency_ms')} ms  input {s.get('input_ref')}"
        )
    if kind == "tool_action":
        mode = "read" if s.get("read_only") else "write"
        return f"tool {s.get('tool')} ({mode})  {s.get('status')}  key {s.get('logical_key')}  result {s.get('result_ref')}"
    if kind == "tool_refused":
        return f"tool {s.get('tool')}  REFUSED {s.get('status')}  {s.get('error')}"
    if kind == "verification_recorded":
        checks = ", ".join(f"{k} {v}" for k, v in (s.get("checks") or {}).items())
        unv = f"  unverified {s['unverified']}" if s.get("unverified") else ""
        return f"verify             {s.get('status')}  ({checks}){unv}"
    if kind == "budget_reminder":
        return f"budget reminder    tier {s.get('tier')}  remaining {s.get('remaining')}"
    return f"{kind}  {s.get('payload')}"
