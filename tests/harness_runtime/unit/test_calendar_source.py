"""Calendar input validation and the half-open watch window."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.personal.calendar_source import MockCalendarSource
from harness.personal.calendar_types import PersonalWindowEvent

T0 = datetime(2026, 11, 25, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parents[1] / "fixtures/personal/calendar.json"


def test_mock_calendar_window_bounds(tmp_path):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    change = document["changes"][0]
    document["changes"] = [
        {**change, "version": hour + 1, "occurred_at": (T0 + timedelta(hours=hour)).isoformat()}
        for hour in range(4)
    ]
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    result = MockCalendarSource().changes(path, T0, T0 + timedelta(hours=2))
    assert [c.version for c in result] == [2, 3]
    assert result[0].body == change["body"]


@pytest.mark.parametrize("field,value", [
    ("item_id", ""), ("item_id", "x" * 101), ("version", 0), ("kind", "deleted"),
    ("title", "x" * 81), ("summary", "x" * 301), ("body", "x" * 2001),
    ("occurred_at", "2026-11-25T01:00:00"), ("start", "2026-11-26T02:00:00"),
    ("end", "2026-11-26T04:00:00"), ("previous", {"start": "2026-11-27T02:00:00"}),
    ("unexpected", True),
])
def test_mock_calendar_rejects_invalid_change(tmp_path, field, value):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["changes"][0][field] = value
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        MockCalendarSource().changes(path, T0, T0 + timedelta(hours=2))
    assert str(path) in str(error.value)


@pytest.mark.parametrize("content", [None, "{broken", '{"calendar_id":"lab","changes":[],"extra":1}'])
def test_mock_calendar_file_errors_include_path(tmp_path, content):
    path = tmp_path / "invalid.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError) as error:
        MockCalendarSource().changes(path, T0, T0 + timedelta(hours=2))
    assert str(path) in str(error.value)


@pytest.mark.parametrize("kind", ["created", "updated", "cancelled"])
def test_calendar_accepts_kinds_and_optional_previous(tmp_path, kind):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["changes"][0].update(kind=kind, previous=None, body="x" * 2000)
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    change, = MockCalendarSource().changes(path, T0, T0 + timedelta(hours=2))
    assert change.kind == kind and change.previous is None and len(change.body) == 2000


@pytest.mark.parametrize("overrides", [
    {"since": "2026-11-25T00:00:00"}, {"until": "2026-11-25T01:00:00"},
    {"since": T0 + timedelta(hours=1)}, {"since": T0 + timedelta(hours=2)},
    {"kind": "schedule"}, {"extra": True},
])
def test_personal_window_rejects_invalid_payload(overrides):
    values = dict(kind="personal_window", subscription_id="sub_1", since=T0, until=T0 + timedelta(hours=1))
    with pytest.raises(ValidationError):
        PersonalWindowEvent.model_validate(values | overrides)
