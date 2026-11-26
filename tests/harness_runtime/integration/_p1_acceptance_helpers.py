"""Local HTTP peer and test-only entry point for real Worker and Scheduler processes."""

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import subprocess
import sys
from tempfile import mkdtemp
from threading import Lock, Thread
import time
from unittest.mock import patch
from uuid import uuid4

import httpx

from harness.app import build_system
from harness.config import HarnessConfig
from harness.contracts import (
    Actor, CheckResult, ReconcileResult, StaleLease, TaskOutcome, TaskStatus, ToolResult, ToolSpec,
    VerificationStatus,
)
from harness.results.verifier import DEFAULT_CHECKERS, artifact_readable
from harness.runtime.models import ScriptedModelAdapter, result_step, tool_step
from harness.tools.connectors.files import FilesReadConnector, p0_connectors
from harness.worker import _submit_step


class Peer(ThreadingHTTPServer):
    def __init__(self):
        self.entries = []
        self.receipts = {}
        self.lock = Lock()
        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_port}"

    def record(self, path, key, body):
        with self.lock:
            self.entries.append({"path": path, "key": key, "body": body})
            if path == "/execute":
                self.receipts.setdefault(key, (body["mode"], f"receipt-{uuid4().hex}"))
            if path == "/read":
                return {"recorded": True}
            mode, receipt = self.receipts[key]
            if path == "/reconcile" and mode == "manual":
                return {"status": "unsupported"}
            return {"status": "succeeded", "receipt_ref": receipt}

    def requests(self, path):
        with self.lock:
            return [entry for entry in self.entries if entry["path"] == path]

    def for_action(self, action):
        with self.lock:
            return [entry for entry in self.entries if entry["key"] == action]

    def receipt(self, action):
        with self.lock:
            return self.receipts[action][1]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        reply = self.server.record(self.path, self.headers["Idempotency-Key"], body)
        if self.path == "/execute" and body["mode"] in ("auto", "manual"):
            # The service has accepted the operation. Withhold the HTTP response
            # beyond the connector's read timeout, then close the connection.
            time.sleep(2.5)
            return
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        # The structured request journal above is the evidence for this peer.
        pass


@contextmanager
def external_service():
    with Peer() as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


@contextmanager
def worker_child(env, directory, mode, url):
    with subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), mode, url],
        env=env, cwd=mkdtemp(prefix=f"{mode}-", dir=directory),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def marker(process):
    readable, _, _ = select.select([process.stdout], [], [], 30)
    assert readable, "worker did not reach the requested boundary"
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return json.loads(line)


def release(process):
    process.stdin.write("continue\n")
    process.stdin.flush()


def finish(process):
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, stderr
    return json.loads(stdout)


async def exchange(url, path, action, body):
    async with httpx.AsyncClient(base_url=url, timeout=httpx.Timeout(5, read=1.0), trust_env=False) as client:
        response = await client.post(path, json=body, headers={"Idempotency-Key": action})
        return response.raise_for_status().json()


class ExternalWrite:
    spec = ToolSpec(
        name="acceptance.write", description="Deliver the draft to the acceptance HTTP peer",
        input_schema={"type": "object", "properties": {"mode": {"type": "string"},
                      "message": {"type": "string"}}, "required": ["mode", "message"]},
        read_only=False, side_effects=True, idempotent=True, supports_reconcile=True,
    )

    def __init__(self, url):
        self.url = url

    async def execute(self, call):
        try:
            reply = await exchange(self.url, "/execute", call.action_id, call.args)
        except httpx.ReadTimeout as exc:
            # The gateway accepts the standard TimeoutError from connectors.
            raise TimeoutError("peer accepted the request but did not reply in time") from exc
        return ToolResult(status=reply["status"], receipt_ref=reply["receipt_ref"], summary="delivered")

    async def reconcile(self, action):
        reply = await exchange(self.url, "/reconcile", action.action_id, {
            "action": action.action_id, "request_hash": action.request_hash,
        })
        return ReconcileResult(**reply)


class ObservedRead(FilesReadConnector):
    def __init__(self, store, url):
        super().__init__(store)
        self.url = url

    async def execute(self, call):
        result = await super().execute(call)
        await exchange(self.url, "/read", call.action_id, {
            "key": call.logical_key, "attempt": call.attempt_id, "file": Path(call.args["path"]).name,
            "status": result.status.value,
        })
        return result


def read_input(context):
    refs = [line.removeprefix("- file:") for msg in context.messages if msg["role"] == "user"
            for line in msg["content"].splitlines() if line.startswith("- file:")]
    return tool_step(("files.read", {"path": refs[-1]}))


def draft_from_read(context):
    reply = context.messages[-1]
    assert reply["role"] == "tool" and reply["name"] == "files.read"
    return write_draft(reply["content"])


def write_draft(content):
    return tool_step(("artifacts.write", {"name": "draft", "content": f"# Draft\n## Goal\n{content}\n## Plan\nReview."}))


def model_for(mode):
    submit = _submit_step("done", {"draft": "@draft"}, result_step)
    write = write_draft("Prepare the draft.")
    scripts = {
        "scheduler": [], "stale": [], "finish": [write, submit],
        "pause-after-read": [read_input, draft_from_read, submit],
        "write-from-read": [draft_from_read, submit],
        "input": [read_input, draft_from_read, submit],
    }
    if mode in ("send", "auto", "manual"):
        return ScriptedModelAdapter([tool_step(("acceptance.write", {"mode": mode, "message": "draft ready"}))])
    return ScriptedModelAdapter(scripts[mode])


async def check_source(spec, ctx):
    if not any(Path(ref).name == "supplement.md" for ref in ctx.task.input_refs):
        return CheckResult(id=spec.id, status=VerificationStatus.INCONCLUSIVE, detail="supplement not supplied")
    return await artifact_readable(spec, ctx)


async def wait_for_parent(**ready):
    print(json.dumps(ready), flush=True)
    assert await asyncio.to_thread(sys.stdin.readline) == "continue\n"


def pause_after_read(system):
    checkpoint = system.repos.tasks.checkpoint

    async def save(lease, ref, now):
        task = await checkpoint(lease, ref, now)
        reader = Actor(scope_id=task.scope_id, actor_id=task.actor_id)
        saved = json.loads((await system.artifacts.read(ref, reader)).content)["checkpoint"]
        if saved["stage"] == "tools_completed":
            await wait_for_parent(attempt=lease.attempt_id, checkpoint=ref)
        return task

    system.repos.tasks.checkpoint = save


def probe_old_lease(system, probes):
    run = system.runner.run

    async def paused_run(context, profile, execution, *, lease_lost):
        lease = execution.lease
        await wait_for_parent(attempt=lease.attempt_id, epoch=lease.lease_epoch)
        cancelled = TaskOutcome(status=TaskStatus.CANCELLED)
        for name, operation in (("heartbeat", system.tasks.heartbeat(lease)),
                                ("commit", system.tasks.commit(lease, cancelled))):
            try:
                await operation
            except StaleLease as exc:
                probes[name] = type(exc).__name__
        return await run(context, profile, execution, lease_lost=lease_lost)

    system.runner.run = paused_run


async def main(mode, url):
    def connectors(store):
        original = p0_connectors(store)
        return [ObservedRead(store, url) if c.spec.name == "files.read" else c for c in original] + [ExternalWrite(url)]

    checkers = {"artifact_readable": check_source} if mode == "input" else {}
    with patch("harness.app.p0_connectors", connectors), patch.dict(DEFAULT_CHECKERS, checkers):
        system = build_system(HarnessConfig.from_env(), model_for(mode))
    system.policy.roles["main_tools@1"] |= {ExternalWrite.spec.name}
    system.policy.capabilities["document_draft@1"] |= {ExternalWrite.spec.name}
    probes = {}
    async with system.lifespan():
        if mode == "scheduler":
            return {"reclaimed": await system.scheduler.tick()}
        if mode == "pause-after-read":
            pause_after_read(system)
        if mode == "stale":
            probe_old_lease(system, probes)
        report = await system.worker().run_once()
        assert report, "worker found no queued task"
        return {**asdict(report), "probes": probes}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(*sys.argv[1:]))), flush=True)
