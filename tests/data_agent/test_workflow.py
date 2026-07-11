from agent.data_agent.workflow import compile_workflow, draft_workflow, save_workflow, list_workflows


def test_draft_workflow_compiles_parallel_outputs():
    plan = draft_workflow(
        "Analyze sales, build a dashboard and executive data story using the policy document",
        tables=["orders", "customers"],
        knowledge_sources=["kb_policy"],
    )
    assert plan["validation"]["ok"] is True
    assert {node["kind"] for node in plan["nodes"]} >= {
        "source_read",
        "knowledge_search",
        "query",
        "dashboard",
        "story",
    }
    assert plan["optimizer"]["max_parallelism"] >= 2


def test_compile_workflow_detects_cycle():
    compiled = compile_workflow(
        {
            "workflow_id": "wf_cycle",
            "goal": "bad plan",
            "nodes": [
                {"id": "a", "kind": "query", "depends_on": ["b"]},
                {"id": "b", "kind": "report", "depends_on": ["a"]},
            ],
        }
    )
    assert compiled["validation"]["ok"] is False
    assert any(issue["code"] == "cycle" for issue in compiled["validation"]["issues"])


def test_compile_workflow_rebuilds_edges_after_edit():
    compiled = compile_workflow(
        {
            "workflow_id": "wf_edit",
            "goal": "edited plan",
            "nodes": [
                {"id": "source", "kind": "source_read", "depends_on": []},
                {"id": "query", "kind": "query", "depends_on": ["source"]},
                {"id": "story", "kind": "story", "depends_on": ["query"]},
            ],
            "edges": [{"from": "stale", "to": "edge"}],
        }
    )
    assert compiled["edges"] == [
        {"from": "source", "to": "query"},
        {"from": "query", "to": "story"},
    ]


def test_workflow_library_is_profile_scoped():
    plan = draft_workflow("Summarize orders", tables=["orders"])
    save_workflow(plan)
    records = list_workflows()
    assert records[0]["workflow_id"] == plan["workflow_id"]
    assert records[0]["validation_ok"] is True
