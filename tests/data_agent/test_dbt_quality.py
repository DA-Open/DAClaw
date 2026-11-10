"""auto-dbt: value-aware planning, degenerate-output checks and one repair pass."""

from agent.data_agent import dbt_engine
from agent.data_agent.engine import DataEngine


def _engine():
    eng = DataEngine()
    eng.con.execute(
        "CREATE TABLE feedback AS SELECT * FROM (VALUES "
        "('T1', 'Excellent', 'Very high'), ('T2', 'Excellent', 'Very high'), "
        "('T3', 'Qualified', 'Relatively high'), ('T4', 'Qualified', 'average'), "
        "('T5', 'Qualified', 'Relatively low')) "
        "AS t(\"Transaction Number\", \"Quality result\", \"Repurchase intention\")"
    )
    eng._table_source["feedback"] = "test"
    return eng


# The bug this guards against: the planner never saw the real values, mapped
# a 4-level scale onto 'yes'/'no', and every row became 'unknown'.
_GUESSED_SQL = (
    "SELECT \"Quality result\" AS quality, CASE WHEN LOWER(\"Repurchase intention\") "
    "IN ('yes', '是') THEN 'yes' WHEN LOWER(\"Repurchase intention\") IN ('no', '否') "
    "THEN 'no' ELSE 'unknown' END AS repurchase FROM source('feedback')"
)
_MART_SQL = (
    "SELECT quality, COUNT(*) AS n, COUNT(*) FILTER (WHERE repurchase = 'yes') AS yes_n, "
    "COUNT(*) FILTER (WHERE repurchase = 'yes')::DOUBLE / NULLIF(COUNT(*) FILTER "
    "(WHERE repurchase IN ('yes', 'no')), 0) AS yes_rate "
    "FROM ref('int_classified') GROUP BY quality"
)
_FIXED_SQL = (
    "SELECT \"Quality result\" AS quality, CASE WHEN \"Repurchase intention\" IN "
    "('Very high', 'Relatively high') THEN 'yes' WHEN \"Repurchase intention\" IN "
    "('average', 'Relatively low') THEN 'no' ELSE 'unknown' END AS repurchase "
    "FROM source('feedback')"
)


def _plan():
    return {
        "project": "p",
        "models": [
            {"name": "int_classified", "layer": "intermediate", "materialized": "table", "sql": _GUESSED_SQL},
            {"name": "mart_quality", "layer": "marts", "materialized": "table", "sql": _MART_SQL},
        ],
    }


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def chat_json(self, messages, **_kw):
        self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0)


def _patch_llm(monkeypatch, replies):
    fake = _FakeLLM(replies)
    monkeypatch.setattr(dbt_engine, "get_llm", lambda config=None: fake)
    return fake


def test_schema_brief_lists_real_values():
    eng = _engine()
    eng.con.execute("CREATE TABLE stg_old AS SELECT 1 AS x")
    eng._table_source["stg_old"] = "__dbt__"
    brief = dbt_engine._schema_brief(eng)
    assert (
        "Repurchase intention: 'Very high' (2), 'Relatively high' (1), "
        "'Relatively low' (1), 'average' (1)"
    ) in brief
    assert "Transaction Number: 'T1' (1)" in brief  # 5 distinct <= 12 → listed
    assert "- stg_old(x INTEGER)" in brief  # earlier models are listed ...
    assert "x: " not in brief  # ... but not profiled


def test_schema_brief_samples_high_cardinality_columns(monkeypatch):
    monkeypatch.setattr(dbt_engine, "_PROFILE_MAX_DISTINCT", 3)
    brief = dbt_engine._schema_brief(_engine())
    assert "Transaction Number: 5 distinct, e.g. " in brief


def test_quality_checks_flag_degenerate_columns():
    eng = _engine()
    eng.con.execute(
        "CREATE TABLE m AS SELECT * FROM (VALUES ('a', NULL, 'unknown', 0, 3), "
        "('b', NULL, 'unknown', 0, 4)) AS t(k, rate, label, missing_n, n)"
    )
    eng.con.execute("CREATE TABLE s AS SELECT NULL AS raw_col FROM range(3)")
    executed = [
        {"name": "m", "layer": "marts", "status": "ok", "row_count": 2},
        {"name": "s", "layer": "staging", "status": "ok", "row_count": 3},
    ]
    issues = dbt_engine._quality_issues(eng, executed)
    by_issue = {(i["severity"], i["issue"]) for i in issues}
    assert ("error", "column 'rate' is NULL in all 2 rows") in by_issue
    assert ("error", "column 'label' is 'unknown' in all 2 rows") in by_issue
    assert ("warning", "column 'missing_n' is 0 in all 2 rows") in by_issue
    assert not any(i["model"] == "s" for i in issues)  # staging passes raw through
    assert len(executed[0]["warnings"]) == 3


def test_repair_pass_fixes_guessed_literals(monkeypatch):
    fake = _patch_llm(monkeypatch, [
        _plan(),
        {"explanation": "used the real scale", "models": [{"name": "int_classified", "sql": _FIXED_SQL}]},
    ])
    res = dbt_engine.run_dbt(_engine(), "quality vs repurchase")

    # the planner and the repair pass both saw the real values
    assert all("'Relatively high'" in p for p in fake.prompts)
    assert "is 'unknown' in all 5 rows" in fake.prompts[1]

    rep = res["repair"]
    assert rep["attempted"] and rep["changed"] == ["int_classified"]
    assert any(i["severity"] == "error" for i in rep["issues_before"])
    assert res["quality_issues"] == []
    rows = {r[0]: r for r in res["final_result"]["rows"]}
    assert rows["Excellent"][2] == 2 and rows["Excellent"][3] == 1.0
    assert rows["Qualified"][2] == 1
    assert next(m for m in res["models"] if m["name"] == "int_classified")["repaired"]


def test_repair_ignores_unknown_models_and_unsafe_sql(monkeypatch):
    _patch_llm(monkeypatch, [
        _plan(),
        {"models": [
            {"name": "not_a_model", "sql": "SELECT 1"},
            {"name": "int_classified", "sql": "DROP TABLE feedback"},
        ]},
    ])
    res = dbt_engine.run_dbt(_engine(), "goal")
    assert res["repair"]["changed"] == []
    assert any(i["severity"] == "error" for i in res["quality_issues"])
    assert res["n_ok"] == 2


def test_repair_failure_keeps_original_result(monkeypatch):
    fake = _patch_llm(monkeypatch, [_plan()])  # second call raises IndexError
    res = dbt_engine.run_dbt(_engine(), "goal")
    assert "error" in res["repair"] and res["n_ok"] == 2
    assert len(fake.prompts) == 2


def test_repair_can_be_disabled(monkeypatch):
    fake = _patch_llm(monkeypatch, [_plan()])
    res = dbt_engine.run_dbt(_engine(), "goal", repair=False)
    assert res["repair"] == {"attempted": False}
    assert len(fake.prompts) == 1
    assert any(i["severity"] == "error" for i in res["quality_issues"])


def test_clean_pipeline_makes_no_repair_call(monkeypatch):
    plan = _plan()
    plan["models"][0]["sql"] = _FIXED_SQL
    fake = _patch_llm(monkeypatch, [plan])
    res = dbt_engine.run_dbt(_engine(), "goal")
    assert res["repair"] == {"attempted": False}
    assert res["quality_issues"] == []
    assert len(fake.prompts) == 1
