"""Offline Q1-Q3 demo on a real database; no paid model calls or external sends.

PYTHONPATH=src python scripts/harness/proactive_data_card_demo.py --database /data/example.sqlite --output /tmp/card-demo
The scripted adapter copies the gateway's persisted profile output. It makes no
semantic inferences; all tools, verification, task commits and handoff are real.
"""
import argparse
import asyncio
import json
from pathlib import Path

from harness.app import build_system
from harness.config import HarnessConfig
from harness.contracts import Actor, Event, EventType, Subscription, TaskOrigin, TaskRequest, Usage
from harness.runtime.models import result_step, tool_step


class ProfileScript:
    model = "scripted-data-card"
    max_output_tokens = 1024

    def __init__(self, database):
        self.database = database
        self.system = None
        self.step = 0

    async def next_step(self, context, *, max_output_tokens=None):
        self.step += 1
        if self.step == 1:
            answer = tool_step(("data.profile", {"path": str(self.database)}))
        else:
            actions = await self.system.repos.actions.list_for_task(context.task_id)
            last = actions[-1]
            if last.status != "succeeded":
                raise RuntimeError(f"demo tool failed: {last.tool} ({last.status})")
            actor = Actor(scope_id="demo", actor_id="demo")
            output = json.loads((await self.system.artifacts.read(last.result_ref, actor)).content)
            if self.step == 2:
                card = {"schema_version": "1", "subject_ref": str(self.database), "data_version": "demo-v1",
                        "meaning_source": "computed", "profile_action_id": last.id,
                        "profile": output["data"]["profile"], "inferences": []}
                answer = tool_step(("artifacts.write", {"name": "data_card", "media_type": "application/json",
                                                       "content": json.dumps(card, ensure_ascii=False, indent=2)}))
            elif self.step == 3:
                answer = result_step("Verified data profile", {"data_card": output["artifact_refs"][0]})
            else:
                raise RuntimeError("demo script exhausted")
        return answer.model_copy(update={"usage": Usage(model_calls=1)})


async def run(database, output):
    database = database.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    model = ProfileScript(database)
    system = build_system(HarnessConfig(artifact_root=output / "artifacts"), model,
        policy_spec={"users": {"demo": {r: {"paths": [str(database)]} for r in ("main", "proactive")}}})
    model.system = system
    sub = await system.proactive.create_subscription(Subscription(scope_id="demo", owner_id="demo",
        goal="Understand the dataset and check general data quality", subject_ref=str(database), resources=[str(database)],
        capability_ref="data_card@1", event_filter={"source": "demo"}))
    refs = await system.proactive.on_event(Event(scope_id="demo", type=EventType.EXTERNAL, source="demo",
        source_event_id="demo-v1", subject_ref=str(database), payload={"kind": "data_connected",
        "data_version": "demo-v1", "fingerprint": "demo-v1"}))
    report = await system.worker(lanes=["background"]).run_once()
    if report.status != "succeeded":
        raise RuntimeError(str(report))
    actor = Actor(scope_id="demo", actor_id="demo")
    content = await system.artifacts.read(report.result_ref, actor)
    (output / "data-card.json").write_bytes(content.content)
    main = await system.tasks.create(TaskRequest(request_key="main", goal="Describe this dataset",
        subject_ref=str(database), capability_ref="document_draft@2"), actor,
        TaskOrigin(agent_role="main", agent_profile_ref="main@1", run_lane="foreground", trigger_type="user"))
    lease = await system.tasks.claim("demo-main", ["foreground"])
    context = await system.builder.build(main, system.agents.resolve_ref("main@1"), lease)
    items = await system.delivery.inbox("demo")
    if len(items) != 1 or len(context.injected_materials) != 1:
        raise RuntimeError("demo did not publish and inject the data card")
    read = await system.delivery.read("demo", items[0].id)
    await system.repos.delivery.ack("demo", items[0].id, "handled", "useful", system.delivery.clock())
    checks = (await system.repos.verifications.list_for_task(report.task_id))[-1]
    evidence = {"task_id": report.task_id, "status": report.status, "verification": checks.model_dump(mode="json"),
        "table_count": len(read["card"]["profile"]["tables"]), "finding_count": len(read["card"]["profile"]["findings"]),
        "inbox_mode": items[0].mode, "injected_materials": [m.model_dump() for m in context.injected_materials],
        "decision": (await system.repos.subscriptions.decisions("demo", sub.id))[0].model_dump(mode="json"),
        "model": model.model, "external_messages_sent": 0}
    (output / "acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": evidence["status"], "verification": checks.status, "tables": evidence["table_count"],
                      "findings": evidence["finding_count"], "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.database, args.output))
