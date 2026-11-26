"""P1 acceptance through real HTTP, independent workers and a shared PostgreSQL ledger."""

import asyncio
from collections import Counter
from contextlib import contextmanager
import json
from pathlib import Path
import signal
import sys
import time
from uuid import uuid4

import pytest
from sqlalchemy import select

from harness.storage.postgres import PgRepositories
from harness.storage.schema import usage_reservations

from ._p1_acceptance_helpers import external_service, finish, marker, release, worker_child
from .test_processes import AUTH_A, api_server, process_setup  # noqa: F401 -- shared PG fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals (SIGKILL/SIGSTOP/SIGCONT) and pipe select"),
]
USAGE = ("model_calls", "tool_calls", "input_tokens", "output_tokens")


@contextmanager
def observations(record_property):
    data = {}
    try:
        yield data
    finally:
        record_property("acceptance_evidence", json.dumps(data, ensure_ascii=False))


class Processes:
    def __init__(self, env, body, directory, external):
        self.env = env
        self.body = body
        self.directory = directory
        self.external = external

    @contextmanager
    def api(self, name):
        with api_server(self.env, self.directory / name) as client:
            client.headers.update(AUTH_A)
            yield client

    def worker(self, mode):
        return worker_child(self.env, self.directory, mode, self.external.url)

    def run(self, mode):
        with self.worker(mode) as process:
            return finish(process)

    def recover(self):
        return self.run("scheduler")["reclaimed"]

    def ledger(self, task):
        return asyncio.run(read_ledger(self.env["DARAOS_HARNESS_DATABASE_DSN"], task))


@pytest.fixture
def lab(process_setup, tmp_path):
    env, body, _ = process_setup
    env.update(DARAOS_HARNESS_LEASE_TTL_SEC="2", DARAOS_HARNESS_HEARTBEAT_INTERVAL_SEC="1",
               DARAOS_HARNESS_RECONCILE_GRACE_SEC="1")
    with external_service() as external:
        yield Processes(env, body, tmp_path, external)


async def read_ledger(dsn, task_id):
    repos = PgRepositories.from_dsn(dsn)
    try:
        task = await repos.tasks.get("s1", task_id)
        actions = await repos.actions.list_for_task(task_id)
        async with repos.engine.connect() as conn:
            rows = (await conn.execute(select(usage_reservations).where(
                usage_reservations.c.task_id == task_id
            ))).mappings().all()
        return {
            "checkpoint": task.checkpoint_ref,
            "actions": {a.id: {"tool": a.tool, "key": a.logical_key, "status": a.status.value,
                               "receipt": a.receipt_ref} for a in actions},
            "charges": [(r["attempt_id"], r["status"], r["settled"]) for r in rows],
        }
    finally:
        await repos.close()


def submit(api, body, key):
    response = api.post("/tasks", json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def state(api, task):
    return api.get(f"/tasks/{task}").raise_for_status().json()


def trace(api, task):
    return api.get(f"/tasks/{task}/trace").raise_for_status().json()


def steps(history, kind):
    return [step for step in history["steps"] if step["type"] == kind]


def artifact(api, ref):
    return api.get(f"/artifacts/{ref}").raise_for_status()


def approve(api, task, action, decision="approve"):
    return api.post(f"/tasks/{task}/approvals/{action['action_id']}", json={
        "decision": decision, "request_hash": action["request_hash"],
    })


def parked(lab, api, task, mode):
    attempt = lab.run(mode)
    current = state(api, task)
    assert attempt["committed"] and attempt["task_id"] == task
    assert (current["status"], current["wait_condition"]) == ("waiting", "approval")
    action, = current["pending_approvals"]
    assert action["tool"] == "acceptance.write" and action["request_hash"]
    assert lab.external.for_action(action["action_id"]) == []
    return action


def unknown(lab, api, task, mode, observed):
    action = parked(lab, api, task, mode)
    assert approve(api, task, action).status_code == 200
    assert lab.run("finish")["status"] == "waiting"
    current = state(api, task)
    assert (current["status"], current["wait_condition"]) == ("waiting", "reconciliation")
    item, = current["unsettled_actions"]
    assert (item["action_id"], item["status"]) == (action["action_id"], "unknown")
    assert [r["path"] for r in lab.external.for_action(action["action_id"])] == ["/execute"]
    timed_out, = [s for s in steps(trace(api, task), "tool_action")
                  if s["action_id"] == item["action_id"] and s["status"] == "unknown"]
    error = artifact(api, timed_out["result_ref"]).json()["error"]["code"]
    observed.update(wait=current["wait_condition"], timeout=error)
    assert error == "deadline_exceeded"
    return item


def test_p1_worker_kill_reclaim_and_resume_across_processes(lab, record_property):
    with observations(record_property) as seen, lab.api("api") as api:
        task = submit(api, lab.body, "kill")
        with lab.worker("pause-after-read") as first:
            ready = marker(first)
            stored = lab.ledger(task)["checkpoint"]
            checkpoint = artifact(api, stored).json()["checkpoint"]
            seen.update(task=task, checkpoint=stored, stage=checkpoint["stage"], first=ready["attempt"])
            assert stored == ready["checkpoint"] and checkpoint["stage"] == "tools_completed"
            assert len(checkpoint["completed_action_ids"]) == 1 and checkpoint["pending_calls"] == []
            first.kill()
            seen["exit"] = first.wait(timeout=10)
            assert seen["exit"] == -signal.SIGKILL
        time.sleep(int(lab.env["DARAOS_HARNESS_LEASE_TTL_SEC"]) + 0.1)
        seen["reclaimed"] = lab.recover()
        seen["after_reclaim"] = state(api, task)["status"]
        assert (seen["reclaimed"], seen["after_reclaim"]) == (1, "queued")
        second = lab.run("write-from-read")
        current = state(api, task)
        history = trace(api, task)
        seen.update(second=second["attempt_id"], status=current["status"],
                    exits=[a["exit_reason"] for a in history["attempts"]])
        assert second["committed"] and current["status"] == "succeeded"
        assert seen["exits"] == ["lease_expired", "succeeded"] and seen["first"] != seen["second"]
        result = artifact(api, current["result_ref"])
        brief = Path(lab.body["input_refs"][0].removeprefix("file:")).read_text()
        seen.update(artifact_http=result.status_code, draft=result.text)
        assert brief in result.text
        ledger = lab.ledger(task)
        read, = [s for s in steps(history, "tool_action") if s["tool"] == "files.read"]
        seen["read"] = {"action": read["action_id"], "key": read["logical_key"],
                        "executions": lab.external.for_action(read["action_id"])}
        assert [i for i, a in ledger["actions"].items() if a["key"] == read["logical_key"]] == [read["action_id"]]
        assert checkpoint["completed_action_ids"] == [read["action_id"]]
        assert [r["body"]["key"] for r in seen["read"]["executions"]] == [read["logical_key"]]
        assert [r["key"] for r in lab.external.requests("/read")] == [read["action_id"]]
        per_attempt = {}
        for attempt in (seen["first"], seen["second"]):
            charges = Counter()
            for owner, status, actual in ledger["charges"]:
                assert status == "settled"
                if owner == attempt:
                    charges.update(actual)
            models = [s for s in steps(history, "model_call") if s["attempt_id"] == attempt]
            tools = [s for s in steps(history, "tool_action") if s["attempt_id"] == attempt]
            measured = {"model_calls": len(models), "tool_calls": len(tools),
                        "input_tokens": sum(s["input_tokens"] for s in models),
                        "output_tokens": sum(s["output_tokens"] for s in models)}
            assert dict(charges) == measured
            per_attempt[attempt] = measured
        seen.update(per_attempt=per_attempt, total=current["usage"])
        assert [(v["model_calls"], v["tool_calls"]) for v in per_attempt.values()] == [(1, 1), (2, 1)]
        assert {k: sum(v[k] for v in per_attempt.values()) for k in USAGE} == {k: current["usage"][k] for k in USAGE}
        assert current["usage"]["total_tokens"] == current["usage"]["input_tokens"] + current["usage"]["output_tokens"]


def test_p1_duplicate_submit_and_approval_execute_once(lab, record_property):
    with observations(record_property) as seen:
        key = "restart-repeat"
        with lab.api("before-restart") as api:
            task = submit(api, lab.body, key)
        with lab.api("after-restart") as api:
            repeated = submit(api, lab.body, key)
            conflict = api.post("/tasks", json={**lab.body, "goal": "changed"}, headers={"Idempotency-Key": key})
            seen.update(task=task, repeated=repeated, submit_http=[202, 202, conflict.status_code])
            assert repeated == task and conflict.status_code == 409
            action = parked(lab, api, task, "send")
            decisions = [approve(api, task, action, d).status_code for d in ("approve", "approve", "reject")]
            events = steps(trace(api, task), "approval_decided")
            seen.update(action=action["action_id"], request_hash=action["request_hash"], approval_http=decisions,
                        decision_events=[e["payload"]["decision"] for e in events])
            assert decisions == [200, 200, 409] and seen["decision_events"] == ["approve"]
            seen["status"] = lab.run("finish")["status"]
            seen["http"] = lab.external.for_action(action["action_id"])
            assert seen["status"] == state(api, task)["status"] == "succeeded"
            assert [(r["path"], r["key"]) for r in seen["http"]] == [("/execute", action["action_id"])]
            assert len(lab.external.requests("/execute")) == 1


def test_p1_cancel_running_waiting_and_unknown_cleanup(lab, record_property):
    with observations(record_property) as seen, lab.api("api") as api:
        waiting = submit(api, lab.body, "cancel-waiting")
        action = parked(lab, api, waiting, "send")
        status = api.post(f"/tasks/{waiting}/cancel").raise_for_status().json()["status"]
        refused = approve(api, waiting, action)
        seen["approval"] = {"status": status, "action": lab.ledger(waiting)["actions"][action["action_id"]]["status"],
                            "approve_http": refused.status_code, "execute": len(lab.external.requests("/execute"))}
        assert seen["approval"] == {"status": "cancelled", "action": "failed", "approve_http": 409, "execute": 0}
        assert state(api, waiting)["pending_approvals"] == []

        running = submit(api, lab.body, "cancel-running")
        with lab.worker("pause-after-read") as worker:
            marker(worker)
            before = lab.ledger(running)["actions"]
            prior_calls = [s["call_id"] for s in steps(trace(api, running), "model_call")]
            assert state(api, running)["status"] == "running"
            cancelling = api.post(f"/tasks/{running}/cancel").raise_for_status().json()["status"]
            release(worker)
            finished = finish(worker)
        after = lab.ledger(running)["actions"]
        history = trace(api, running)
        later_calls = [s["call_id"] for s in steps(history, "model_call")]
        seen["running"] = {"request": cancelling, "status": finished["status"], "before": before, "after": after,
                           "model_calls_before": prior_calls, "model_calls_after": later_calls}
        assert [a["tool"] for a in before.values()] == ["files.read"]
        assert (cancelling, finished["status"]) == ("cancelling", "cancelled") and after == before
        assert len(prior_calls) == 1 and later_calls == prior_calls and not steps(history, "tool_refused")
        assert len(steps(history, "tool_action")) == 1 and not lab.external.requests("/execute")

        task = submit(api, lab.body, "cancel-unknown")
        cleanup = seen["unknown"] = {}
        item = unknown(lab, api, task, "auto", cleanup)
        assert api.post(f"/tasks/{task}/cancel").raise_for_status().json()["status"] == "cancelled"
        pending = state(api, task)
        cleanup.update(before=pending["cleanup"], unsettled=pending["unsettled_actions"])
        assert pending["cleanup"] == "pending_reconciliation" and pending["unsettled_actions"] == [item]
        time.sleep(int(lab.env["DARAOS_HARNESS_RECONCILE_GRACE_SEC"]))
        lab.recover()
        done = state(api, task)
        changes = [s["to"] for s in steps(trace(api, task), "task_status_changed")]
        requests = lab.external.for_action(item["action_id"])
        cleanup.update(after=done["cleanup"], status=done["status"], transitions=changes, http=requests)
        assert (done["status"], done["cleanup"], done["unsettled_actions"]) == ("cancelled", "done", [])
        assert changes[changes.index("cancelled"):] == ["cancelled"]
        assert [r["path"] for r in requests] == ["/execute", "/reconcile"]


def test_p1_stale_worker_cannot_heartbeat_or_commit(lab, record_property):
    with observations(record_property) as seen, lab.api("api") as api:
        task = submit(api, lab.body, "fence")
        with lab.worker("stale") as first:
            ready = marker(first)
            first.send_signal(signal.SIGSTOP)
            time.sleep(int(lab.env["DARAOS_HARNESS_LEASE_TTL_SEC"]) + 0.1)
            assert lab.recover() == 1 and state(api, task)["status"] == "queued"
            winner = lab.run("finish")
            before = state(api, task)
            assert before["status"] == "succeeded" and winner["result_ref"] == before["result_ref"]
            first.send_signal(signal.SIGCONT)
            release(first)
            stale = finish(first)
        after = state(api, task)
        seen.update(old=ready, winner=winner["attempt_id"], rejections=stale["probes"],
                    committed=stale["committed"], before={k: before[k] for k in ("result_ref", "status", "version")},
                    after={k: after[k] for k in ("result_ref", "status", "version")})
        assert ready["attempt"] == stale["attempt_id"] != winner["attempt_id"]
        assert stale["probes"] == {"heartbeat": "StaleLease", "commit": "StaleLease"}
        assert stale["lease_lost"] and not stale["committed"] and seen["before"] == seen["after"]
        assert artifact(api, after["result_ref"]).status_code == 200


def test_p1_unknown_side_effect_reconciles_without_blind_retry(lab, record_property):
    with observations(record_property) as seen, lab.api("api") as api:
        for mode in ("auto", "manual"):
            task = submit(api, lab.body, mode)
            observed = seen[mode] = {"task": task}
            item = unknown(lab, api, task, mode, observed)
            action = item["action_id"]
            observed["before"] = item
            time.sleep(int(lab.env["DARAOS_HARNESS_RECONCILE_GRACE_SEC"]))
            lab.recover()
            if mode == "manual":
                current = state(api, task)
                unsettled, = current["unsettled_actions"]
                observed["manual"] = unsettled["needs_manual_confirmation"]
                assert current["wait_condition"] == "reconciliation" and observed["manual"] is True
                response = api.post(f"/tasks/{task}/actions/{action}/resolve", json={"outcome": "happened"})
                observed["resolve_http"] = response.status_code
                assert response.status_code == 200 and response.json()["status"] == "succeeded"
            assert state(api, task)["status"] == "queued"
            settled = lab.ledger(task)["actions"][action]
            observed.update(settled=settled, status=lab.run("finish")["status"], http=lab.external.for_action(action))
            observed["reconciliations"] = [s["payload"]["status"] for s in steps(trace(api, task), "tool_reconcile")
                                           if s["payload"]["action_id"] == action]
            assert settled["status"] == "succeeded" and settled["receipt"]
            if mode == "auto":
                assert settled["receipt"] == lab.external.receipt(action)
                assert observed["reconciliations"] == ["succeeded"]
            else:
                assert artifact(api, settled["receipt"]).json()["outcome"] == "happened"
                assert observed["reconciliations"] == ["unsupported", "succeeded"]
            assert observed["status"] == state(api, task)["status"] == "succeeded"
            assert [(r["path"], r["key"]) for r in observed["http"]] == [("/execute", action), ("/reconcile", action)]
        assert len(lab.external.requests("/execute")) == 2


def test_p1_waiting_input_resume_across_processes(lab, record_property):
    with observations(record_property) as seen, lab.api("api") as api:
        task = submit(api, lab.body, "more-input")
        first = lab.run("input")
        before = state(api, task)
        seen.update(first=first["attempt_id"], initial=before["status"], wait=before["wait_condition"])
        assert (first["status"], before["wait_condition"]) == ("waiting", "input")
        assert lab.ledger(task)["checkpoint"]
        source = lab.directory / "material" / "supplement.md"
        contents = f"Updated acceptance value {uuid4().hex}"
        source.write_text(contents, encoding="utf-8")
        version = before["version"]
        url = f"/tasks/{task}/resume"

        def resume(v, path):
            return api.post(url, json={"expected_version": v, "input_refs": [f"file:{path}"]})

        stale = resume(version - 1, source)
        denied = resume(version, lab.directory / "outside.md")
        seen["errors"] = [[r.status_code, r.json()["error_code"]] for r in (stale, denied)]
        assert seen["errors"] == [[409, "version_conflict"], [400, "forbidden"]]
        assert state(api, task)["version"] == version
        accepted = resume(version, source)
        seen.update(resume_http=accepted.status_code, resumed=accepted.json()["status"])
        assert (accepted.status_code, seen["resumed"]) == (200, "queued")
        second = lab.run("input")
        history = trace(api, task)
        calls = [s for s in steps(history, "model_call") if s["attempt_id"] == second["attempt_id"]]
        messages = artifact(api, calls[0]["input_ref"]).json()["context"]["messages"]
        user_lines = [line for m in messages if m["role"] == "user" for line in m["content"].splitlines()]
        reads = lab.external.requests("/read")
        text = artifact(api, second["result_ref"]).text
        seen.update(second=second["attempt_id"], status=second["status"], supplied=source.name,
                    supplied_line_count=user_lines.count(f"- file:{source}"), reads=reads, artifact=text,
                    verifications=[s["status"] for s in steps(history, "verification_recorded")])
        assert first["attempt_id"] != second["attempt_id"] and seen["status"] == state(api, task)["status"] == "succeeded"
        assert seen["supplied_line_count"] == 1 and reads[-1]["body"]["file"] == source.name
        assert reads[-1]["body"]["attempt"] == second["attempt_id"] and contents in text
        assert seen["verifications"] == ["inconclusive", "passed"]
