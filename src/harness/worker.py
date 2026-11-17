"""``python -m harness.worker`` -- run the harness worker.

    python -m harness.worker --once --submit task.json --model-config model.yaml
    python -m harness.worker --once --submit task.json --scripted script.json

With ``DARAOS_HARNESS_DATABASE_DSN``, this worker claims tasks submitted by
an API process using the same database. Both processes need the same
absolute artifact root and input paths. Without a DSN the store is in
memory: ``--submit`` creates a task in this process first (main role,
trusted origin). ``--once`` runs one attempt with either backend.

``task.json`` is the POST /tasks body (spec 5.1) plus ``request_key`` and the
caller's ``scope_id`` / ``actor_id``::

    {"request_key": "k1", "scope_id": "s1", "actor_id": "u1",
     "goal": "根据材料写项目说明草稿，不发布",
     "input_refs": ["file:/data/s1/brief.md"],
     "capability_ref": "document_draft@1",
     "acceptance": {...}, "limits": {...}}

``model.yaml`` holds ``ModelConfig`` fields (``provider``, ``model``,
``base_url``, ``api_key_env`` ...); keys are read from the environment
variable it names, never from the file. ``--scripted`` runs a fixed script of
tool calls instead of a model, for smoke tests without network.

Other settings come from ``DARAOS_HARNESS_*`` (see config.py), including
``DARAOS_HARNESS_POLICY_FILE`` for the permissions YAML (see app.py).
The command prints one JSON line per attempt and exits 0 when the last
attempt was committed, 1 otherwise. ``--trace`` then prints the task's
timeline (see trace.py) to stderr, or ``--trace FILE.json`` writes it as JSON;
it reads records already written and costs no model calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from .app import build_system
from .config import HarnessConfig
from .contracts import Actor, AgentRole, RunLane, TaskOrigin, TaskRequest, TriggerType
from .trace import build_trace, render_trace


def _load(path: str) -> Any:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return yaml.safe_load(text) if p.suffix in (".yaml", ".yml") else json.loads(text)


def _model(args: argparse.Namespace):
    from .runtime.models import ModelConfig, ScriptedModelAdapter, create_adapter, result_step, tool_step

    if args.scripted:
        steps = []
        for entry in _load(args.scripted):
            if "submit" in entry:
                refs = entry["submit"].get("artifacts", {})
                steps.append(_submit_step(entry["submit"].get("summary", "done"), refs, result_step))
            else:
                steps.append(tool_step((entry["tool"], entry.get("args", {}))))
        return ScriptedModelAdapter(steps)
    if not args.model_config:
        raise SystemExit("give --model-config or --scripted")
    return create_adapter(ModelConfig(**_load(args.model_config)))


def _submit_step(summary: str, refs: dict[str, str], result_step):
    """A scripted submit that may name an artifact by the name it was written
    under ("@draft"), since its ID is only known at run time."""

    def step(context):
        resolved = {}
        for name, ref in refs.items():
            if isinstance(ref, str) and ref.startswith("@"):
                wanted = ref[1:]
                written = [
                    m for m in context.messages
                    if m.get("role") == "tool" and f"wrote artifact {wanted!r} as " in m.get("content", "")
                ]
                if not written:
                    raise ValueError(f"no artifact named {wanted!r} was written")
                ref = written[-1]["content"].split(" as ")[1].split(" ")[0]
            resolved[name] = ref
        return result_step(summary, resolved)

    return step


async def _main(args: argparse.Namespace) -> int:
    config = HarnessConfig.from_env()
    async with build_system(config, _model(args)).lifespan() as system:
        if args.submit:
            body = dict(_load(args.submit))
            actor = Actor(actor_id=body.pop("actor_id", "local"), scope_id=body.pop("scope_id"))
            background = bool(body.pop("prefer_background", False))
            request = TaskRequest(**body, prefer_background=background)
            origin = TaskOrigin(
                agent_role=AgentRole.MAIN,
                agent_profile_ref="main@1",
                run_lane=RunLane.BACKGROUND if background else RunLane.FOREGROUND,
                trigger_type=TriggerType.USER,
            )
            task = await system.tasks.create(request, actor, origin)
            print(json.dumps({"submitted": task.id, "status": str(task.status)}, ensure_ascii=False), flush=True)

        lanes = [RunLane(l) for l in args.lanes]
        worker = system.worker(lanes=lanes)
        if not args.once:
            stop = asyncio.Event()
            await worker.run_forever(stop=stop)
            return 0
        report = await worker.run_once()
        if report is None:
            print(json.dumps({"claimed": None}), flush=True)
            return 1
        line = {
            "task_id": report.task_id,
            "attempt_id": report.attempt_id,
            "status": str(report.status) if report.status else None,
            "committed": report.committed,
            "result_ref": report.result_ref,
            "error": report.error,
            "lease_lost": report.lease_lost,
        }
        if report.result_ref and args.print_result:
            content = await system.artifacts.read(report.result_ref, Actor(actor_id="cli", scope_id=report.scope_id))
            line["result"] = content.content.decode("utf-8", errors="replace")
        print(json.dumps(line, ensure_ascii=False), flush=True)
        if args.trace is not None:
            trace = await build_trace(system.repos, system.artifacts, report.scope_id, report.task_id)
            if args.trace == "-":
                print(render_trace(trace), file=sys.stderr, flush=True)
            else:
                Path(args.trace).write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if report.committed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.worker", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run one attempt and exit")
    parser.add_argument("--submit", metavar="TASK_JSON", help="optionally create this task before claiming")
    parser.add_argument("--model-config", metavar="YAML", help="ModelConfig fields")
    parser.add_argument("--scripted", metavar="JSON", help="scripted steps instead of a model")
    parser.add_argument("--lanes", nargs="+", default=["foreground"], choices=["foreground", "background"])
    parser.add_argument("--print-result", action="store_true", help="include the result artifact's text")
    parser.add_argument(
        "--trace", nargs="?", const="-", metavar="FILE.json",
        help="after the attempt, print the task's timeline to stderr, or write it as JSON to FILE.json",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if not args.once and args.submit is None and args.model_config is None and args.scripted is None:
        parser.error("nothing to do")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
