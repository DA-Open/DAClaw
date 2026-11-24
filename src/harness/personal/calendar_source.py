"""Read a small, local calendar fixture outside storage transactions."""
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from .calendar_types import CalendarChange, CalendarFile


class MockCalendarSource:
    def changes(self, path: str | Path, since: datetime, until: datetime) -> list[CalendarChange]:
        try:
            calendar = CalendarFile.model_validate_json(Path(path).read_bytes())
        except (FileNotFoundError, ValidationError) as exc:
            raise ValueError(f"Invalid calendar {path}: {exc}") from exc
        return [change for change in calendar.changes if since < change.occurred_at <= until]
