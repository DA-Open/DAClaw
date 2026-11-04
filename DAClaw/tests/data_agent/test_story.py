from agent.data_agent.artifacts import ResultTable
from agent.data_agent.story import render_story_html


def test_story_is_self_contained_and_grounded():
    result = ResultTable(
        artifact_id="res_test",
        kind="sql_result",
        columns=[{"name": "month", "type": "VARCHAR"}, {"name": "revenue", "type": "DOUBLE"}],
        rows=[["2026-01", 12.0], ["2026-02", 18.0]],
        row_count=2,
    )
    html = render_story_html(
        "Revenue pulse",
        [
            {"type": "kpi", "title": "Highlights", "items": [{"label": "Peak", "value": "18"}]},
            {"type": "chart", "title": "Trend", "result": result, "chart_type": "line"},
        ],
    )
    assert "DAClaw <i>Story</i>" in html
    assert "Revenue pulse" in html
    assert "story_chart_1" in html
    assert "setInterval" in html
