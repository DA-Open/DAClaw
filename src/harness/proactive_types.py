"""Payloads used by the data-card trigger and in-app delivery."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .contracts import _Model, new_id, utcnow


class DataEvent(_Model):
    kind: Literal["data_connected", "data_updated", "schedule"]
    data_version: str = Field(min_length=1, max_length=200)
    fingerprint: str = Field(min_length=1, max_length=200)


class InboxItem(_Model):
    id: str = Field(default_factory=lambda: new_id("inbox"))
    scope_id: str
    subscription_id: str
    task_id: str
    result_ref: str
    subject_ref: str
    data_version: str
    topic: str
    keywords: list[str] = Field(default_factory=list)
    mode: Literal["silent", "digest", "notify"]
    score: int = Field(ge=0, le=9)
    dedup_key: str
    valid_until: datetime
    status: Literal["pending", "read", "handled", "ignored", "cancelled"] = "pending"
    notice_reserved: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class NoticeRecord(_Model):
    scope_id: str
    dedup_key: str
    task_id: str
    channel: str
    severity: int
    valid_until: datetime
