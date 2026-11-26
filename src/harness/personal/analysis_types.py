"""Types for analysis watch: the source event, the baseline body, the recompute."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ..contracts import _Model


class SourceChangedEvent(_Model):
    """A trusted caller says this file's content changed.

    subject_ref is the path and source_event_id is "<path>@<sha256>", so a
    redelivery of the same version is deduplicated by the events table.
    """

    kind: Literal["source_changed"] = "source_changed"
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)


class AnalysisWatch(_Model):
    """What to watch on a baseline: one row, one column, one threshold."""

    query_index: int = Field(ge=0)
    column: str = Field(min_length=1)
    min_relative_change: float = Field(gt=0)


class BaselineSource(_Model):
    path: str
    sha256: str
    size_bytes: int
    kind: str
    table: str
    tables: dict[str, str]
    row_count: int


class BaselineQuery(_Model):
    query_ref: str
    sql: str
    columns: list[str]
    row_count: int
    row_count_capped: bool = False
    values: list[list[Any]] = Field(default_factory=list)


class BaselineBody(_Model):
    """A data answer that can be rechecked: who asked, on which bytes, how."""

    task_id: str
    actor_id: str
    question: str
    sources: list[BaselineSource]
    queries: list[BaselineQuery]
    answer: str = ""


class RecomputeQuery(_Model):
    query_index: int
    query_ref: str
    old_values: list[list[Any]]
    new_values: list[list[Any]]
    row_count: int


class RecomputeThreshold(_Model):
    query_index: int
    column: str
    old: float | None = None
    new: float | None = None
    delta: float | None = None
    relative_change: float | None = None
    exceeds_threshold: bool = False


class RecomputeResult(_Model):
    new_sha256: str
    queries: list[RecomputeQuery] = Field(default_factory=list)
    threshold: RecomputeThreshold | None = None
    error: str = ""


__all__ = [
    "AnalysisWatch",
    "BaselineBody",
    "BaselineQuery",
    "BaselineSource",
    "RecomputeQuery",
    "RecomputeResult",
    "RecomputeThreshold",
    "SourceChangedEvent",
]
