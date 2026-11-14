"""Manual smoke test: one real model drives the Runner end to end.

Not part of the test suite (it calls a paid API). The tools and the Verifier
are the fakes from ``tests/harness_runtime``; the model is real. It prints
the trajectory so a person can read it.

    DARAOS_HARNESS_MODEL_PROVIDER=custom \
    DARAOS_HARNESS_MODEL_NAME=<model> \
    DARAOS_HARNESS_MODEL_BASE_URL=<https://.../v1> \
    DARAOS_HARNESS_MODEL_API_KEY_ENV=OPENAI_API_KEY \
    python scripts/harness_runner_smoke.py [--max-model-calls 6]

Any provider preset works the same way, e.g. PROVIDER=deepseek NAME=deepseek-chat.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from harness.contracts import Acceptance, EventType, Limits, ToolOutcome, ToolStatus, utcnow  # noqa: E402
from harness.context.builder import SimpleContextBuilder, StaticInstructions, StaticToolCatalog  # noqa: E402
from harness.runtime.models import ModelConfig, create_adapter  # noqa: E402
from harness.runtime.runner import AgentRunner  # noqa: E402
from harness.storage.memory import InMemoryRepositories  # noqa: E402
from tests.harness_runtime.fakes import (  # noqa: E402
    MAIN_PROFILE,
    READ,
    WRITE,
    FakeGateway,
    FakeVerifier,
    MemoryArtifactStore,
    claimed_task,
)

NOTES = """\
# Meeting notes 2026-11-12
- DaraOS branch is the integration branch; all changes go through PRs.
- Harness P0: contracts done (PR-A), Runner and budget next (PR-D).
- Budget reminder: state remaining calls and tokens, never tell the model to hurry.
- Verifier decides passed / failed / inconclusive; the model only proposes a result.
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-model-calls", type=int, default=6)
    args = parser.parse_args()

    config = ModelConfig.from_env()
    print("model:", json.dumps(config.safe_dict(), ensure_ascii=False))
    repos = InMemoryRepositories()
    store = MemoryArtifactStore(repos)

    def read(call):
        if call.args.get("path") == "notes.md":
            return {"summary": NOTES}
        return ToolOutcome(status=ToolStatus.FAILED, summary=f"no such file: {call.args.get('path')}")

    gateway = FakeGateway(repos, store, {READ.name: read})
    runner = AgentRunner(repos, create_adapter(config), gateway, FakeVerifier(repos), store)
    task, execution = await claimed_task(
        repos,
        now=utcnow(),
        goal="Read notes.md and write a short summary of it as an artifact named 'draft' (3-5 bullet points).",
        limits=Limits(max_model_calls=args.max_model_calls, max_tool_calls=10, max_total_tokens=60_000, timeout_sec=600),
        acceptance=Acceptance(required_artifacts=["draft"]),
    )
    builder = SimpleContextBuilder(
        repos,
        instructions=StaticInstructions({"main@1": "You are the Main agent of DaraOS. You write short documents."}),
        tools=StaticToolCatalog([READ, WRITE]),
    )
    context = await builder.build(task, MAIN_PROFILE, execution.lease)
    outcome = await runner.run(context, MAIN_PROFILE, execution)

    print("\n== trajectory")
    for event in await repos.events.list("s1", task_id=task.id, limit=1000):
        if event.type is EventType.MODEL_CALL:
            out = store.json(event.payload["output_ref"])
            if "error" in out:
                print(f"model  error: {out['error']}")
                continue
            what = [f"{c['tool']}({json.dumps(c['args'], ensure_ascii=False)[:80]})" for c in out["tool_calls"]]
            if out["kind"] == "candidate_result":
                what = [f"submit: {out['candidate']['summary'][:120]!r} {out['candidate']['artifact_refs']}"]
            usage = event.payload["usage"]
            print(f"model  {event.payload['latency_ms']:>6} ms  in={usage['input_tokens']:>5} out={usage['output_tokens']:>4}  {'; '.join(what)}")
        elif event.type is EventType.BUDGET_REMINDER:
            print(f"remind {event.payload['tier']}: {event.payload['text']}")
    print("\n== outcome")
    print(outcome.model_dump_json(indent=2, exclude_none=True))
    draft = [a for a in await repos.artifacts.list_for_task("s1", task.id) if a.name == "draft"]
    if draft:
        print("\n== draft\n" + store.content[draft[-1].id].decode())
    committed = await repos.tasks.commit(execution.lease, outcome, utcnow())
    print("\ncommitted status:", committed.status, " usage:", committed.usage.model_dump())
    return 0 if committed.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
