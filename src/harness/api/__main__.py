"""``python -m harness.api`` -- serve the harness HTTP API.

    python -m harness.api --host 127.0.0.1 --port 8090
    python -m harness.api --with-worker --model-config model.yaml

Settings come from ``DARAOS_HARNESS_*`` (config.py); API callers and their
scopes come from ``tokens`` in the permissions file
(``DARAOS_HARNESS_POLICY_FILE``). With no tokens configured every request is
401, so the API is closed by default.

Until the PostgreSQL store is wired in (item 2b) tasks live in this
process's memory, so a worker must run here too: ``--with-worker`` starts
one alongside the server (needs ``--model-config`` or ``--scripted``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

from ..app import build_system, load_policy_file
from ..config import HarnessConfig
from ..contracts import RunLane
from .app import create_app
from .auth import TokenAuth

log = logging.getLogger("harness.api")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.api", description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1, local only)")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--with-worker", action="store_true", help="run a worker in this process")
    parser.add_argument("--lanes", nargs="+", default=["foreground", "background"], choices=["foreground", "background"])
    parser.add_argument("--model-config", metavar="YAML", help="ModelConfig fields, for --with-worker")
    parser.add_argument("--scripted", metavar="JSON", help="scripted steps instead of a model, for --with-worker")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    import uvicorn

    from ..worker import _model

    config = HarnessConfig.from_env()
    spec = load_policy_file(config.policy_file)
    auth = TokenAuth(spec.get("tokens") or [])
    if not len(auth):
        log.warning("no API tokens in the permissions file: every request will be refused (401)")
    if args.with_worker and not (args.model_config or args.scripted):
        parser.error("--with-worker needs --model-config or --scripted")
    model = _model(args) if args.with_worker else _NoModel()
    system = build_system(config, model, policy_spec=spec)
    lifespan = None
    if args.with_worker:
        worker = system.worker(lanes=[RunLane(l) for l in args.lanes])

        @contextlib.asynccontextmanager
        async def lifespan(_app):
            stop = asyncio.Event()
            running = asyncio.create_task(worker.run_forever(stop=stop))
            try:
                yield
            finally:
                stop.set()
                await running

    app = create_app(system, auth, lifespan=lifespan)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info" if args.verbose else "warning")
    return 0


class _NoModel:
    """Model for an API-only process: it never runs tasks."""

    model = "none"

    async def next_step(self, *args, **kwargs):  # pragma: no cover - never called without a worker
        raise RuntimeError("this process runs no worker")


if __name__ == "__main__":
    sys.exit(main())
