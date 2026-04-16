"""Tests for data source adapter profiling."""

from pathlib import Path

import pytest

from agent.data_sources import DataSourceError, available_data_adapters, profile_data_source


def _write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_available_adapters_includes_csv():
    assert "csv" in available_data_adapters()


def test_profile_csv_basic(tmp_path):
    csv_path = _write_csv(
        tmp_path / "sales.csv",
        "id,amount,region\n1,10.5,north\n2,20,south\n3,,west\n",
    )

    result = profile_data_source(str(csv_path), sample_rows=2, max_rows=1000)

    assert result["adapter"] == "csv"
    assert result["rows_profiled"] == 3
    assert result["truncated"] is False
    assert result["columns"] == ["id", "amount", "region"]
    assert len(result["sample_rows"]) == 2
    assert result["column_stats"]["id"]["inferred_type"] == "number"
    assert result["column_stats"]["amount"]["null_count"] == 1


def test_profile_csv_respects_max_rows(tmp_path):
    csv_path = _write_csv(
        tmp_path / "big.csv",
        "a,b\n1,2\n3,4\n5,6\n7,8\n",
    )

    result = profile_data_source(str(csv_path), sample_rows=2, max_rows=2)

    assert result["rows_profiled"] == 2
    assert result["truncated"] is True
    assert len(result["sample_rows"]) == 2


def test_profile_unsupported_extension_raises(tmp_path):
    data_path = _write_csv(tmp_path / "notes.json", '{"a": 1}\n')

    with pytest.raises(DataSourceError, match="Unsupported data source format"):
        profile_data_source(str(data_path))

