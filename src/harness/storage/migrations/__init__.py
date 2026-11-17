"""Alembic migrations for the harness tables.

Run them with ``python -m harness.storage.migrate`` (see migrate.py). There is
no alembic.ini: the config is built in code and the database URL comes from
``DARAOS_HARNESS_DATABASE_DSN``, so no URL or password is ever written to a
file in the repo.
"""
