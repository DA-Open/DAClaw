"""Data source adapters for Data Agent workflows.

This module provides a small abstraction layer so DAClaw can expose one
data-analysis entry point while supporting multiple backends (CSV now, SQL
later) behind the scenes.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Set


class DataSourceError(ValueError):
    """Raised when a data source cannot be profiled."""


class DataSourceAdapter(Protocol):
    """Interface for data source profiling adapters."""

    name: str

    def supports(self, source: str) -> bool:
        """Return True when this adapter can handle *source*."""

    def profile(
        self,
        source: str,
        *,
        sample_rows: int = 10,
        max_rows: int = 100_000,
    ) -> Dict[str, object]:
        """Return a structured profile for *source*."""


@dataclass
class _ColumnAccumulator:
    total: int = 0
    nulls: int = 0
    non_nulls: int = 0
    numeric_count: int = 0
    numeric_sum: float = 0.0
    numeric_min: Optional[float] = None
    numeric_max: Optional[float] = None
    unique_values: Set[str] = field(default_factory=set)
    unique_capped: bool = False

    def update(self, raw_value: object, unique_cap: int) -> None:
        self.total += 1
        if raw_value is None:
            self.nulls += 1
            return

        text = str(raw_value).strip()
        if text == "":
            self.nulls += 1
            return

        self.non_nulls += 1
        if not self.unique_capped:
            self.unique_values.add(text)
            if len(self.unique_values) > unique_cap:
                self.unique_values.clear()
                self.unique_capped = True

        parsed = _parse_number(text)
        if parsed is None:
            return

        self.numeric_count += 1
        self.numeric_sum += parsed
        self.numeric_min = parsed if self.numeric_min is None else min(self.numeric_min, parsed)
        self.numeric_max = parsed if self.numeric_max is None else max(self.numeric_max, parsed)

    def to_summary(self) -> Dict[str, object]:
        inferred_type = "text"
        if self.non_nulls == 0:
            inferred_type = "empty"
        elif self.numeric_count == self.non_nulls:
            inferred_type = "number"
        elif self.numeric_count > 0 and (self.numeric_count / self.non_nulls) >= 0.9:
            inferred_type = "mostly_number"

        summary: Dict[str, object] = {
            "total_rows_seen": self.total,
            "non_null_count": self.non_nulls,
            "null_count": self.nulls,
            "inferred_type": inferred_type,
        }

        if self.unique_capped:
            summary["unique_count"] = f">{_UNIQUE_CAP}"
            summary["unique_count_capped"] = True
        else:
            summary["unique_count"] = len(self.unique_values)
            summary["unique_count_capped"] = False

        if self.numeric_count:
            summary["numeric"] = {
                "count": self.numeric_count,
                "min": self.numeric_min,
                "max": self.numeric_max,
                "mean": round(self.numeric_sum / self.numeric_count, 6),
            }

        return summary


_UNIQUE_CAP = 2_000


def _parse_number(text: str) -> Optional[float]:
    normalized = text.replace(",", "")
    try:
        return float(normalized)
    except (TypeError, ValueError):
        return None


def _resolve_source_path(source: str) -> Path:
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _sniff_dialect(path: Path) -> csv.Dialect:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
    if not sample:
        raise DataSourceError(f"Data source is empty: {path}")

    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        class _FallbackDialect(csv.Dialect):
            delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
            quotechar = '"'
            doublequote = True
            skipinitialspace = False
            lineterminator = "\n"
            quoting = csv.QUOTE_MINIMAL

        return _FallbackDialect()


class CsvAdapter:
    """Profile local CSV/TSV files."""

    name = "csv"
    _SUPPORTED_SUFFIXES = {".csv", ".tsv"}

    def supports(self, source: str) -> bool:
        suffix = Path(source).suffix.lower()
        return suffix in self._SUPPORTED_SUFFIXES

    def profile(
        self,
        source: str,
        *,
        sample_rows: int = 10,
        max_rows: int = 100_000,
    ) -> Dict[str, object]:
        if sample_rows <= 0:
            raise DataSourceError("sample_rows must be greater than 0")
        if max_rows <= 0:
            raise DataSourceError("max_rows must be greater than 0")

        path = _resolve_source_path(source)
        if not path.exists():
            raise DataSourceError(f"Data source not found: {path}")
        if not path.is_file():
            raise DataSourceError(f"Data source is not a file: {path}")

        dialect = _sniff_dialect(path)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, dialect=dialect)
            columns = list(reader.fieldnames or [])
            if not columns:
                raise DataSourceError(f"No header row found in: {path}")

            accumulators = {name: _ColumnAccumulator() for name in columns}
            preview_rows: List[Dict[str, object]] = []

            processed_rows = 0
            truncated = False
            for row in reader:
                processed_rows += 1
                if processed_rows > max_rows:
                    truncated = True
                    break
                if len(preview_rows) < sample_rows:
                    preview_rows.append({name: row.get(name) for name in columns})
                for name in columns:
                    accumulators[name].update(row.get(name), unique_cap=_UNIQUE_CAP)

        return {
            "adapter": self.name,
            "source": str(path),
            "delimiter": dialect.delimiter,
            "rows_profiled": processed_rows,
            "truncated": truncated,
            "columns": columns,
            "column_stats": {
                name: accumulator.to_summary()
                for name, accumulator in accumulators.items()
            },
            "sample_rows": preview_rows,
        }


_ADAPTERS: List[DataSourceAdapter] = [CsvAdapter()]


def available_data_adapters() -> List[str]:
    """Return adapter names available for profiling."""
    return [adapter.name for adapter in _ADAPTERS]


def profile_data_source(
    source: str,
    *,
    sample_rows: int = 10,
    max_rows: int = 100_000,
) -> Dict[str, object]:
    """Profile *source* using the first adapter that supports it."""
    if not source or not source.strip():
        raise DataSourceError("source is required")

    normalized = source.strip()
    for adapter in _ADAPTERS:
        if adapter.supports(normalized):
            return adapter.profile(
                normalized,
                sample_rows=sample_rows,
                max_rows=max_rows,
            )

    suffix = Path(normalized).suffix.lower() or "(none)"
    raise DataSourceError(
        f"Unsupported data source format: {suffix}. "
        f"Available adapters: {', '.join(available_data_adapters())}"
    )

