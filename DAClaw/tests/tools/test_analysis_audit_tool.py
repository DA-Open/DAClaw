"""Tests for analysis audit helpers and tool wrapper."""

from __future__ import annotations

import json
from unittest.mock import patch

from agent.analysis_audit import (
    audit_analysis_report,
    looks_like_analysis_task,
    organize_analysis_trajectory,
)
from tools.analysis_audit_tool import ANALYSIS_AUDIT_SCHEMA, analysis_audit_tool


def test_analysis_audit_schema_shape():
    assert ANALYSIS_AUDIT_SCHEMA["name"] == "analysis_audit"
    props = ANALYSIS_AUDIT_SCHEMA["parameters"]["properties"]
    assert "final_report" in props
    assert "trajectory_text" in props


def test_organize_analysis_trajectory_includes_tool_names():
    messages = [
        {"role": "user", "content": "Analyze this dataset."},
        {
            "role": "assistant",
            "content": "I will inspect it.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "data_profile", "arguments": '{"source":"sales.csv"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"rows": 10, "columns": ["x"]}'},
    ]
    organized = organize_analysis_trajectory(messages)
    assert "tool:data_profile" in organized
    assert "Analyze this dataset." in organized


def test_looks_like_analysis_task_detects_data_mode():
    assert looks_like_analysis_task(
        "hello",
        [],
        "You are operating in data-analyst mode.",
    )


def test_audit_analysis_report_parses_json_response():
    fake_response = type(
        "Resp",
        (),
        {
            "choices": [
                type(
                    "Choice",
                    (),
                    {
                        "message": type(
                            "Msg",
                            (),
                            {
                                "content": json.dumps(
                                    {
                                        "pass": False,
                                        "score": 0.42,
                                        "faithfulness": "low",
                                        "trajectory_quality": "mixed",
                                        "summary": "numeric mismatch",
                                        "recommended_action": "revise_report",
                                        "issues": [
                                            {
                                                "severity": "high",
                                                "stage": "final_report",
                                                "type": "numeric_mismatch",
                                                "message": "Mismatch",
                                                "evidence": "step 3 vs report",
                                                "suggested_action": "revise_report",
                                            }
                                        ],
                                    }
                                )
                            },
                        )()
                    },
                )()
            ]
        },
    )()

    with patch("agent.analysis_audit.call_llm", return_value=fake_response):
        result = audit_analysis_report(
            user_task="Analyze the csv",
            final_report="Revenue is 12",
            trajectory_text="Step 1 | tool:execute_code\nresult:\nRevenue is 10",
        )

    assert result["audit_status"] == "completed"
    assert result["pass"] is False
    assert result["issues"][0]["type"] == "numeric_mismatch"


def test_analysis_audit_tool_requires_inputs():
    result = json.loads(analysis_audit_tool(final_report="", trajectory_text="x"))
    assert "error" in result


def test_analysis_audit_tool_returns_helper_output():
    with patch(
        "tools.analysis_audit_tool.audit_analysis_report",
        return_value={"pass": True, "audit_status": "completed", "issues": []},
    ):
        result = json.loads(
            analysis_audit_tool(
                user_task="Analyze",
                final_report="All good",
                trajectory_text="Step 1",
            )
        )
    assert result["pass"] is True
