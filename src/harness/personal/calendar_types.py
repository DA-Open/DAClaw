"""Validated calendar changes and scheduler windows."""
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ..contracts import _Model


class CalendarChange(_Model):
    item_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    kind: Literal["created", "updated", "cancelled"]
    occurred_at: AwareDatetime
    title: str = Field(max_length=80)
    summary: str = Field(max_length=300)
    start: AwareDatetime
    end: AwareDatetime
    previous: dict[str, AwareDatetime] | None = None
    body: str = Field(max_length=2000)


class CalendarFile(_Model):
    calendar_id: str
    changes: list[CalendarChange]


class PersonalWindowEvent(_Model):
    kind: Literal["personal_window"]
    subscription_id: str
    since: AwareDatetime
    until: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self):
        if self.since >= self.until:
            raise ValueError("personal window must have since < until")
        return self
