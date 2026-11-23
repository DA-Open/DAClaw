"""Run with python -m harness.scheduler; shares PostgreSQL with API and workers."""
import asyncio

from .app import build_system
from .api.__main__ import _NoModel
from .config import HarnessConfig


async def main():
    config = HarnessConfig.from_env()
    if not config.database_dsn:
        raise ValueError("standalone scheduler requires DARAOS_HARNESS_DATABASE_DSN")
    system = build_system(config, _NoModel())
    async with system.lifespan():
        await system.scheduler.run_forever(stop=asyncio.Event())


if __name__ == "__main__":
    asyncio.run(main())
