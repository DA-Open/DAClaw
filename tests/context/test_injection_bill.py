"""Context-bill instrumentation: schema, recording, aggregation."""

from __future__ import annotations

import pytest

from agent.context import events as inj
from daclaw_state import SCHEMA_VERSION, SessionDB


@pytest.fixture()
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    store.create_session("s1", source="test")
    return store


def test_schema_version_is_7_and_table_exists(db):
    assert SCHEMA_VERSION == 7
    row = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='injection_events'"
    ).fetchone()
    assert row is not None


def test_record_writes_rows_and_bill_aggregates(db):
    written = inj.record(
        "s1",
        [
            inj.InjectionEvent(kind="system_prompt", source="skills_index", text="x" * 1000),
            inj.InjectionEvent(kind="memory_prefetch", source="external_memory", text="y" * 110, turn=1, iteration=2),
            inj.InjectionEvent(kind="memory_prefetch", source="external_memory", text="y" * 110, turn=1, iteration=3),
        ],
        db=db,
    )
    assert written == 3

    bill = {row["source"]: row for row in db.injection_bill("s1")}
    assert bill["skills_index"]["events"] == 1
    assert bill["skills_index"]["chars"] == 1000
    assert bill["skills_index"]["est_tokens"] == int(1000 / inj.CHARS_PER_TOKEN)
    assert bill["external_memory"]["events"] == 2
    assert bill["external_memory"]["est_tokens"] == 2 * int(110 / inj.CHARS_PER_TOKEN)

    by_turn = db.injection_bill_by_turn("s1")
    assert {(r["turn"], r["iteration"]) for r in by_turn} == {(0, 0), (1, 2), (1, 3)}


def test_record_is_noop_without_session_or_events(db):
    assert inj.record(None, [inj.InjectionEvent(kind="system_prompt", text="x")], db=db) == 0
    assert inj.record("s1", [], db=db) == 0
    assert db.injection_bill("s1") == []


def test_record_never_raises_when_store_is_broken():
    class Broken:
        def add_injection_events(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert inj.record("s1", [inj.InjectionEvent(kind="k", text="v")], db=Broken()) == 0


def test_system_prompt_layers_adds_synthetic_remainder(db):
    written = inj.record_system_prompt_layers(
        "s1",
        [("identity", "a" * 100), ("skills_index", "b" * 300)],
        total_chars=500,
        db=db,
    )
    assert written == 3  # identity + skills + other_layers
    rows = {r["source"]: r for r in db.injection_bill("s1")}
    assert rows["identity"]["chars"] == 100
    assert rows["skills_index"]["chars"] == 300
    assert rows["other_layers"]["chars"] == 100


def test_preview_is_truncated_and_full_text_is_off_by_default(db):
    inj.record(
        "s1",
        [inj.InjectionEvent(kind="system_prompt", source="big", text="z" * 5000)],
        db=db,
    )
    row = db._conn.execute(
        "SELECT preview, full_text, sha1 FROM injection_events WHERE source='big'"
    ).fetchone()
    assert len(row["preview"]) == inj.PREVIEW_CHARS
    assert row["full_text"] == ""
    assert len(row["sha1"]) == 16
