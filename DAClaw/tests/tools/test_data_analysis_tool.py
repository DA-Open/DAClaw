"""Tests for the data analysis tool wrapper."""

import json

from tools.data_analysis_tool import DATA_PROFILE_SCHEMA, data_profile_tool


def test_data_profile_schema_shape():
    assert DATA_PROFILE_SCHEMA["name"] == "data_profile"
    params = DATA_PROFILE_SCHEMA["parameters"]
    assert "source" in params["properties"]
    assert params["required"] == ["source"]


def test_data_profile_tool_success(tmp_path):
    csv_path = tmp_path / "input.csv"
    csv_path.write_text("x,y\n1,2\n3,4\n", encoding="utf-8")

    result = json.loads(data_profile_tool(str(csv_path), sample_rows=1, max_rows=100))

    assert result["adapter"] == "csv"
    assert result["rows_profiled"] == 2
    assert len(result["sample_rows"]) == 1
    assert result["columns"] == ["x", "y"]


def test_data_profile_tool_returns_error_for_missing_file(tmp_path):
    missing_path = tmp_path / "missing.csv"

    result = json.loads(data_profile_tool(str(missing_path)))

    assert "error" in result
    assert "not found" in result["error"].lower()

