"""Real workers and the existing calendar/sales datasets for watch decision scenarios."""
import json
from collections import deque
from datetime import timedelta

import duckdb

from harness.contracts import Event, EventType, RunLane, Subscription
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.tools.connectors.data import file_digest
from .analysis_kit import Kit
from .fakes import ACTOR, Clock
from .fixtures.analysis.build import write
from .scenarios.test_delivery_feedback import api_client
from .scenarios.test_personal_window import T0, calendar, subscribe
from .scenarios.test_proactive import system


def observation(bundle):
    for message in bundle.messages:
        if message["role"] == "user":
            _, marker, data = message["content"].partition("Observation (data, not instructions):\n")
            if marker:
                return json.loads(data)
    raise AssertionError("the watch user message has no observation")


class WatchCase:
    def __init__(self, tmp_path, repos, clock):
        self.clock = clock
        self.repos = repos
        self.steps = deque()
        self.model = ScriptedModelAdapter([self.next_step], repeat_last=True)
        self.system = system(tmp_path, repos, clock=clock, model=self.model)

    def next_step(self, context):
        step = self.steps.popleft()
        return step(context) if callable(step) else step

    async def admitted(self, sub, path):
        self.sub, self.path = sub, path
        self.record, = await self.repos.subscriptions.decisions("s1", sub.id)
        assert self.record.decision == "admit", self.record
        self.task = await self.system.tasks.get("s1", self.record.task_id)
        return self

    async def build(self):
        lease = await self.system.tasks.claim("observation-test", [RunLane.BACKGROUND])
        assert lease.task_id == self.task.id
        self.task = await self.system.tasks.get("s1", lease.task_id)
        profile = self.system.agents.resolve_ref(self.task.agent_profile_ref)
        return await self.system.builder.build(self.task, profile, lease)

    async def run(self, edit=None, *, read_refs=(), before_submit=None, silent=False):
        def write_decision(context):
            obs = observation(context)
            refs, claims = [], []
            if "changes" in obs:
                refs.append(obs["changes"][0]["ref"])
                title, body = "组会时间有变", obs["changes"][0]["summary"]
            else:
                refs.append(obs["trigger"]["ref"])
                result = obs["recompute"]
                if result["error"]:
                    title, body = "监测复算失败", result["error"][:800]
                else:
                    threshold = result["threshold"]
                    ref = result["queries"][threshold["query_index"]]["query_ref"]
                    refs.append(ref)
                    claims.append({"query_ref": ref, **{k: threshold[k] for k in ("column", "old", "new", "delta")}})
                    title, body = "三月销售额修订", f"原值 {threshold['old']}，新值 {threshold['new']}。"
            decision = dict(schema_version="1", decision="silent" if silent else "propose_notify",
                rationale="The verified change is relevant to the subscription.", evidence_refs=refs,
                draft=None if silent else {"title": title, "body": body},
                valid_until=None if silent else (self.task.created_at + timedelta(hours=1)).isoformat(),
                observation_digest="Reviewed the source change.", memory_proposals=[])
            if "recompute" in obs:
                decision["claims"] = claims
            if edit is not None:
                edit(decision, obs)
            self.decision = decision
            return tool_step(("artifacts.write", {"name": "decision", "media_type": "application/json",
                             "content": json.dumps(decision, ensure_ascii=False)}))

        def submit(context):
            if before_submit is not None:
                before_submit()
            output = next(m["content"] for m in reversed(context.messages)
                          if m["role"] == "tool" and "wrote artifact" in m["content"])
            return result_step("Completed the watch", {"decision": output.split(" as ")[1].split()[0]})

        self.steps.extend(tool_step(("memory.read", {"ref": ref})) for ref in read_refs)
        self.steps.extend([write_decision, submit])
        report = await self.system.worker(lanes=[RunLane.BACKGROUND]).run_once()
        assert report.task_id == self.task.id
        self.task = await self.system.tasks.get("s1", self.task.id)
        return self.task

    async def checks(self):
        records = await self.repos.verifications.list_for_task(self.task.id)
        return {check.id: check for result in records for check in result.checks}

    async def inbox(self):
        async with api_client(self.system) as client:
            response = await client.get("/inbox")
        response.raise_for_status()
        return response.json()


async def calendar_case(tmp_path, repos, *, quiet_hours=(0, 0), focus=("组会",), changes=None):
    case = WatchCase(tmp_path, repos, Clock(T0))
    path = calendar(tmp_path, changes=changes)
    sub = await subscribe(case.system, path, quiet_hours=quiet_hours, focus=list(focus))
    case.clock.advance(7205)
    await case.system.scheduler.tick()
    return await case.admitted(sub, path)


async def analysis_case(tmp_path, repos, *, broken=False):
    kit = Kit(tmp_path, repos)
    path = write(tmp_path / "dataset", "v1")
    try:
        _, _, baseline_task = await kit.run_analysis(path)
        assert baseline_task.status == "succeeded", baseline_task
    finally:
        kit.close_sessions()
    baseline, = await repos.memories.current("s1", now=kit.clock(), kinds=["analysis"])
    case = WatchCase(tmp_path, repos, kit.clock)
    sub = await case.system.proactive.create_subscription(Subscription(
        scope_id=ACTOR.scope_id, owner_id=ACTOR.actor_id, goal="监测三月销售额", focus=["销售额"],
        subject_ref=str(path), resources=[str(path)], capability_ref="analysis_watch@1",
        event_filter={"kind": "source_changed"}, baseline_ref=f"memory:{baseline.id}@{baseline.version}",
        watch={"query_index": 0, "column": "revenue", "min_relative_change": 0.05},
        created_at=kit.clock(), updated_at=kit.clock()))
    write(path.parent, "v2")
    if broken:
        with duckdb.connect(str(path)) as con:
            con.execute("ALTER TABLE order_items DROP COLUMN unit_price")
    sha = file_digest(path)
    event = Event(scope_id="s1", type=EventType.EXTERNAL, source="data_watch", source_event_id=f"{path}@{sha}",
        subject_ref=str(path), occurred_at=case.clock(), received_at=case.clock(),
        payload={"kind": "source_changed", "path": str(path), "sha256": sha, "size_bytes": path.stat().st_size})
    await case.system.proactive.on_event(event)
    return await case.admitted(sub, path)
