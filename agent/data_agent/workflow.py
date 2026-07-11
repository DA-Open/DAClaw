"""Typed workflow planning for the Data Agent workbench.

The general DAClaw agent remains responsible for execution.  This module adds a
small compiler layer in front of it so the UI can show a transparent plan before
the first tool call, validate the plan deterministically, and expose independent
execution layers.  The draft planner is intentionally deterministic and fast;
runtime tool events are later projected onto these nodes by the frontend.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, deque
from typing import Any, Dict, Iterable, List, Sequence

from daclaw_constants import get_daclaw_home


NODE_SPECS: Dict[str, Dict[str, Any]] = {
    "source_read": {
        "label": "Read data source",
        "description": "Inspect the selected tables and their schemas.",
        "output": "table",
        "tool": "data_connect",
    },
    "knowledge_search": {
        "label": "Search knowledge",
        "description": "Retrieve relevant passages from connected documents.",
        "output": "context",
        "tool": "data_knowledge_search",
    },
    "profile": {
        "label": "Profile data",
        "description": "Measure shape, quality, semantics, and distributions.",
        "output": "profile",
        "tool": "data_profile",
    },
    "query": {
        "label": "Run grounded analysis",
        "description": "Execute read-only SQL and persist the evidence table.",
        "output": "result_table",
        "tool": "data_query",
    },
    "visualize": {
        "label": "Build visualization",
        "description": "Render a query-backed interactive chart.",
        "output": "chart",
        "tool": "data_visualize",
    },
    "dashboard": {
        "label": "Compose dashboard",
        "description": "Combine grounded metrics and charts into a dashboard.",
        "output": "dashboard",
        "tool": "data_dashboard",
    },
    "report": {
        "label": "Write analytical report",
        "description": "Synthesize findings, evidence, caveats, and actions.",
        "output": "report",
        "tool": "data_document",
    },
    "excel": {
        "label": "Produce Excel model",
        "description": "Create or repair a native, editable workbook.",
        "output": "workbook",
        "tool": "data_excel",
    },
    "dbt": {
        "label": "Build data pipeline",
        "description": "Plan and materialize a dependency-aware dbt-style DAG.",
        "output": "pipeline",
        "tool": "data_dbt",
    },
    "story": {
        "label": "Create data story",
        "description": "Turn evidence into an animated executive narrative.",
        "output": "data_story",
        "tool": "data_story",
    },
}


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return value or "step"


def _node(
    node_id: str,
    kind: str,
    *,
    depends_on: Sequence[str] = (),
    description: str | None = None,
    meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    spec = NODE_SPECS[kind]
    return {
        "id": node_id,
        "kind": kind,
        "label": spec["label"],
        "description": description or spec["description"],
        "depends_on": list(depends_on),
        "tool": spec["tool"],
        "output_type": spec["output"],
        "status": "planned",
        "meta": meta or {},
    }


def draft_workflow(
    goal: str,
    *,
    tables: Sequence[str] = (),
    knowledge_sources: Sequence[str] = (),
) -> Dict[str, Any]:
    """Create a fast, deterministic workflow draft from the user's intent."""
    text = (goal or "").strip()
    lower = text.lower()
    nodes: List[Dict[str, Any]] = []

    source_ids: List[str] = []
    selected_tables = list(dict.fromkeys(tables))[:8]
    if selected_tables:
        for table in selected_tables:
            node_id = f"source_{_slug(table)}"
            source_ids.append(node_id)
            nodes.append(
                _node(
                    node_id,
                    "source_read",
                    description=f"Inspect {table} and expose its typed schema.",
                    meta={"table": table},
                )
            )
    else:
        source_ids.append("source_connected_data")
        nodes.append(_node("source_connected_data", "source_read"))

    knowledge_ids: List[str] = []
    wants_knowledge = bool(
        knowledge_sources
        and re.search(r"document|knowledge|policy|pdf|report|research|market|context|文档|知识|政策|研报", lower)
    )
    if wants_knowledge:
        knowledge_ids.append("knowledge_retrieval")
        nodes.append(
            _node(
                "knowledge_retrieval",
                "knowledge_search",
                meta={"sources": list(knowledge_sources)[:8]},
            )
        )

    profile_needed = bool(re.search(r"overview|profile|quality|distribution|summary|概览|画像|质量|分布", lower))
    if profile_needed:
        nodes.append(_node("profile_data", "profile", depends_on=source_ids))

    wants_dbt = bool(re.search(r"\b(dbt|etl|pipeline|staging|mart|warehouse|lineage)\b|管线|数仓|血缘|分层建模", lower))
    wants_excel = bool(re.search(r"excel|xlsx|spreadsheet|workbook|formula|dcf|lbo|表格|工作簿|公式|估值模型", lower))
    wants_story = bool(re.search(r"video|story|presentation|slides|executive brief|数据故事|演示|汇报", lower))
    wants_report = bool(re.search(r"report|memo|brief|recommend|analysis|报告|建议|分析", lower))
    wants_dashboard = bool(re.search(r"dashboard|cockpit|overview|仪表盘|驾驶舱|看板", lower))
    wants_chart = bool(re.search(r"chart|plot|trend|visual|graph|heatmap|图|趋势|可视化|热力", lower))

    if wants_dbt:
        nodes.append(_node("build_pipeline", "dbt", depends_on=source_ids))
        analysis_parent = "build_pipeline"
    else:
        query_deps = list(dict.fromkeys(source_ids + knowledge_ids))
        if profile_needed:
            query_deps.append("profile_data")
        nodes.append(_node("grounded_analysis", "query", depends_on=query_deps))
        analysis_parent = "grounded_analysis"

    output_ids: List[str] = []
    if wants_chart:
        nodes.append(_node("visual_output", "visualize", depends_on=[analysis_parent]))
        output_ids.append("visual_output")
    if wants_dashboard:
        nodes.append(_node("dashboard_output", "dashboard", depends_on=[analysis_parent]))
        output_ids.append("dashboard_output")
    if wants_excel:
        nodes.append(_node("excel_output", "excel", depends_on=[analysis_parent]))
        output_ids.append("excel_output")
    if wants_report:
        deps = [analysis_parent, *output_ids]
        nodes.append(_node("report_output", "report", depends_on=list(dict.fromkeys(deps))))
        output_ids.append("report_output")
    if wants_story:
        deps = [analysis_parent, *output_ids]
        nodes.append(_node("story_output", "story", depends_on=list(dict.fromkeys(deps))))
        output_ids.append("story_output")

    if not output_ids and not wants_dbt:
        nodes[-1]["label"] = "Answer with grounded evidence"

    workflow_id = hashlib.sha1(f"{text}|{'|'.join(selected_tables)}".encode("utf-8")).hexdigest()[:10]
    workflow = {
        "workflow_id": f"wf_{workflow_id}",
        "goal": text,
        "nodes": nodes,
        "edges": [
            {"from": dep, "to": node["id"]}
            for node in nodes
            for dep in node.get("depends_on", [])
        ],
        "selected_tables": selected_tables,
        "knowledge_sources": list(knowledge_sources),
    }
    return compile_workflow(workflow)


def validate_workflow(workflow: Dict[str, Any]) -> Dict[str, Any]:
    nodes = workflow.get("nodes") or []
    issues: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    ids = [str(node.get("id") or "") for node in nodes]
    id_set = set(ids)

    if not nodes:
        issues.append({"code": "empty_workflow", "message": "The workflow contains no nodes."})
    for node_id, count in Counter(ids).items():
        if not node_id:
            issues.append({"code": "missing_node_id", "message": "Every node needs an id."})
        elif count > 1:
            issues.append({"code": "duplicate_node", "message": f"Duplicate node id: {node_id}"})

    for node in nodes:
        kind = node.get("kind")
        node_id = str(node.get("id") or "unknown")
        if kind not in NODE_SPECS:
            issues.append({"code": "unknown_node", "message": f"{node_id} uses unsupported node kind {kind!r}."})
        for dep in node.get("depends_on") or []:
            if dep not in id_set:
                issues.append({"code": "missing_dependency", "message": f"{node_id} depends on missing node {dep}."})

    layers, cyclic = execution_layers(nodes)
    if cyclic:
        issues.append({"code": "cycle", "message": f"Cycle detected among: {', '.join(cyclic)}"})
    if len(nodes) > 12:
        warnings.append({"code": "large_workflow", "message": "Large workflow; consider splitting it into reusable subflows."})
    if layers and max(len(layer) for layer in layers) == 1 and len(nodes) >= 4:
        warnings.append({"code": "fully_serial", "message": "The plan is fully serial; no parallel execution opportunities were found."})
    return {"ok": not issues, "issues": issues, "warnings": warnings}


def execution_layers(nodes: Sequence[Dict[str, Any]]) -> tuple[List[List[str]], List[str]]:
    ids = [str(node.get("id") or "") for node in nodes if node.get("id")]
    id_set = set(ids)
    indegree = {node_id: 0 for node_id in ids}
    outgoing: Dict[str, List[str]] = {node_id: [] for node_id in ids}
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node_id not in id_set:
            continue
        for dep in node.get("depends_on") or []:
            if dep in id_set:
                indegree[node_id] += 1
                outgoing[dep].append(node_id)

    ready = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    layers: List[List[str]] = []
    visited: List[str] = []
    while ready:
        layer = list(ready)
        ready.clear()
        layers.append(layer)
        next_ready: List[str] = []
        for node_id in layer:
            visited.append(node_id)
            for child in outgoing[node_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_ready.append(child)
        ready.extend(sorted(next_ready))
    cyclic = sorted(id_set - set(visited))
    return layers, cyclic


def compile_workflow(workflow: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a workflow and attach validation + optimizer metadata."""
    nodes = workflow.get("nodes") or []
    layers, cyclic = execution_layers(nodes)
    validation = validate_workflow(workflow)
    workflow = dict(workflow)
    workflow["edges"] = [
        {"from": dep, "to": node["id"]}
        for node in nodes
        for dep in node.get("depends_on", [])
    ]
    workflow["validation"] = validation
    workflow["execution_layers"] = layers
    workflow["optimizer"] = {
        "parallel_layers": sum(1 for layer in layers if len(layer) > 1),
        "max_parallelism": max((len(layer) for layer in layers), default=0),
        "critical_path_nodes": len(layers),
        "total_nodes": len(nodes),
        "cyclic_nodes": cyclic,
    }
    return workflow


def get_workflow_dir() -> "Path":
    from pathlib import Path

    path = get_daclaw_home() / "workflows"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_workflow(workflow: Dict[str, Any], *, status: str = "planned") -> Dict[str, Any]:
    """Persist a workflow as a reusable SOP-like template."""
    compiled = compile_workflow(workflow)
    record = dict(compiled)
    record["status"] = status
    record["saved_at"] = time.time()
    path = get_workflow_dir() / f"{record['workflow_id']}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def list_workflows(limit: int = 30) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for path in sorted(get_workflow_dir().glob("wf_*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        records.append(
            {
                "workflow_id": data.get("workflow_id"),
                "goal": data.get("goal"),
                "status": data.get("status"),
                "saved_at": data.get("saved_at"),
                "nodes": len(data.get("nodes") or []),
                "layers": len(data.get("execution_layers") or []),
                "validation_ok": bool((data.get("validation") or {}).get("ok")),
            }
        )
        if len(records) >= limit:
            break
    return records


def workflow_prompt(workflow: Dict[str, Any]) -> str:
    """Compact plan text injected into the execution agent's system prompt."""
    if not workflow or not workflow.get("nodes"):
        return ""
    lines = ["Compiled workflow contract (follow it unless runtime evidence requires a safe correction):"]
    for index, layer in enumerate(workflow.get("execution_layers") or [], start=1):
        labels = []
        by_id = {node["id"]: node for node in workflow["nodes"]}
        for node_id in layer:
            node = by_id[node_id]
            labels.append(f"{node['label']} [{node.get('tool') or node['kind']}]")
        lines.append(f"- Layer {index}: " + " | ".join(labels))
    validation = workflow.get("validation") or {}
    lines.append(f"Validation: {'passed' if validation.get('ok') else 'failed'}")
    return "\n".join(lines)
