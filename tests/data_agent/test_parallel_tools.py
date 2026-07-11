import json
from types import SimpleNamespace

from run_agent import _should_parallelize_tool_batch


def _call(name: str, args: dict):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def test_knowledge_and_single_query_can_run_concurrently():
    calls = [
        _call("data_knowledge_search", {"query": "metric definition"}),
        _call("data_query", {"sql": "SELECT 1"}),
    ]
    assert _should_parallelize_tool_batch(calls) is True


def test_multiple_evidence_writers_stay_sequential():
    calls = [
        _call("data_query", {"sql": "SELECT 1"}),
        _call("data_query", {"sql": "SELECT 2"}),
    ]
    assert _should_parallelize_tool_batch(calls) is False
