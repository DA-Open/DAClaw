"""Alembic environment for the harness: async (asyncpg), ``schema.metadata``.

Two ways in:

* ``migrate.py`` passes a sync connection in ``config.attributes["connection"]``
  (opened by ``AsyncConnection.run_sync``), so tests and the CLI run
  migrations on a connection they own;
* otherwise the URL in ``config.attributes["url"]`` or
  ``DARAOS_HARNESS_DATABASE_DSN`` is used with a fresh async engine.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from harness.storage.postgres import async_url
from harness.storage.schema import metadata

config = context.config


def _url() -> str:
    url = config.attributes.get("url") or os.environ.get("DARAOS_HARNESS_DATABASE_DSN")
    if not url:
        raise RuntimeError("set DARAOS_HARNESS_DATABASE_DSN to run harness migrations")
    return async_url(url)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url())
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    _run(config.attributes["connection"])
else:
    asyncio.run(_run_async())
