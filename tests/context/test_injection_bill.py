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

def test_default_db_follows_the_current_daclaw_home(tmp_path, monkeypatch):
    """The database path is resolved per call, not fixed at import time.

    daclaw_state freezes DEFAULT_DB_PATH when it is imported; if the recorder
    reused that, a process that later points DACLAW_HOME somewhere else (tests,
    benchmark harnesses, profiles) would keep writing to the original file.
    """
    first = tmp_path / "home_a"
    second = tmp_path / "home_b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(inj, "_shared_db", None)
    monkeypatch.setattr(inj, "_shared_db_path", None)

    monkeypatch.setenv("DACLAW_HOME", str(first))
    store_a = inj.default_db()
    assert store_a is not None
    assert str(first) in str(store_a.db_path)

    monkeypatch.setenv("DACLAW_HOME", str(second))
    store_b = inj.default_db()
    assert store_b is not None
    assert str(second) in str(store_b.db_path)
    assert store_b is not store_a

    # Same home twice -> the open handle is reused.
    assert inj.default_db() is store_b


def test_no_injection_log_is_read_at_call_time(db, monkeypatch):
    """DACLAW_NO_INJECTION_LOG must work even when set after import."""
    event = inj.InjectionEvent(kind="system_prompt", source="identity", text="abc")

    monkeypatch.setenv("DACLAW_NO_INJECTION_LOG", "1")
    assert inj.record("s1", [event], db=db) == 0
    assert db.injection_bill("s1") == []

    monkeypatch.delenv("DACLAW_NO_INJECTION_LOG")
    assert inj.record("s1", [event], db=db) == 1


def test_remainder_event_carries_size_without_padding(db):
    inj.record_system_prompt_layers(
        "s1",
        [("identity", "a" * 100)],
        total_chars=350,
        db=db,
    )

    row = db._conn.execute(
        "SELECT chars, est_tokens, preview, full_text, meta_json"
        " FROM injection_events WHERE source = 'other_layers'"
    ).fetchone()
    assert row["chars"] == 250
    assert row["est_tokens"] == int(250 / inj.CHARS_PER_TOKEN)
    # No padding text: nothing to preview, nothing to store.
    assert row["preview"] == ""
    assert row["full_text"] == ""
    assert '"chars": 250' in row["meta_json"].replace("'", '"')


def test_event_size_can_be_declared_without_text(db):
    event = inj.InjectionEvent(kind="system_prompt", source="x", declared_chars=900)
    assert event.chars == 900
    assert event.tokens == int(900 / inj.CHARS_PER_TOKEN)
    assert event.text == ""
