"""Offline re-judge of a finished GAIA2 run from its saved ARE traces.

Same path as ARE's own ``are-benchmark judge`` (ScenarioRunner._judge): the
scenario is reloaded from HF and preprocessed with ``judge_only=True`` (oracle
run + offline validation over all turns), then validated against the agent's
CompletedEvents read from ``<scenario>/are_trace/*.json``. No agent call is made.

  .venv/bin/python -m environments.benchmarks.gaia2.rejudge \
      --src <run_name> --dst <run_name>_rejudge

Writes <dst>/<scenario_id>/rejudge_{orig,fixed}.json with the soft-checker raw
outputs (JUDGE_LOG), and <dst>/rejudge_summary.json.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
from pathlib import Path

from environments.benchmarks.gaia2 import bench
from environments.benchmarks.gaia2 import are_bridge as B


def rejudge_one(row: dict, trace_path: str, cfg: dict, fix_case: bool) -> dict:
    from are.simulation.agents.are_simulation_agent_config import LLMEngineConfig
    from are.simulation.benchmark.scenario_loader import load_scenario
    from are.simulation.data_handler.importer import JsonScenarioImporter
    from are.simulation.environment import Environment
    from are.simulation.scenarios.config import ScenarioRunnerConfig
    from are.simulation.scenarios.scenario_imported_from_json.utils import (
        preprocess_scenario_from_config,
    )
    from are.simulation.types import EventLog

    B.patch_llm_checker(fix_case)
    B.JUDGE_LOG.clear()
    B.JUDGE_ERRORS.clear()
    j = cfg["judge"]
    scenario, _ = load_scenario(row["data"], row["scenario_id"], False)
    rc = ScenarioRunnerConfig(
        model=cfg["agent"]["model"], model_provider="local", judge_only=True,
        judge_engine_config=LLMEngineConfig(
            model_name=j["model"], provider=j["provider"], endpoint=j["endpoint"]),
    )
    preprocess_scenario_from_config(scenario, rc)
    _, completed, _ = JsonScenarioImporter().import_from_json(
        Path(trace_path).read_text(), load_completed_events=True)
    env = Environment()
    env.event_log = EventLog.from_list_view(completed)
    vr = scenario.validate(env)
    agent_events = [e for e in completed if e.event_type.value == "AGENT"]
    return {
        "scenario_id": row["scenario_id"], "mode": "offline", "fix_case": fix_case,
        "nb_turns": scenario.nb_turns,
        "agent_turns_in_trace": sum(1 for e in agent_events
                                    if getattr(e.action, "function_name", "") == "send_message_to_user"),
        "success": bool(vr.success) if vr.success is not None else False,
        "rationale": vr.rationale,
        "error": f"{type(vr.exception).__name__}: {vr.exception}" if vr.exception else None,
        "trace_path": trace_path,
        "judge_log": list(B.JUDGE_LOG),
        "judge_call_retries": list(B.JUDGE_ERRORS),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(bench.HERE / "default.yaml"))
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    args = ap.parse_args()
    cfg = bench.load_cfg(args.config)
    bench.setup_env(cfg)
    logging.basicConfig(level=logging.WARNING)
    runs = Path(cfg["paths"]["bench_root"]) / "runs"
    src, dst = runs / args.src, runs / args.dst
    dst.mkdir(parents=True, exist_ok=True)
    ds = bench.load_rows(cfg)
    idx = {sid: i for i, sid in enumerate(ds["scenario_id"])}
    summary = []
    for res_file in sorted(src.glob("*/result.json")):
        sid = res_file.parent.name
        orig = json.loads(res_file.read_text())
        traces = glob.glob(str(res_file.parent / "are_trace" / "*.json"))
        if orig.get("status", "ok") != "ok" or not traces:
            # error/timeout runs have no scored agent trace to re-judge
            print(f"[skip] {sid} status={orig.get('status')} traces={len(traces)}", flush=True)
            continue
        trace = traces[0]
        row = dict(ds[idx[sid]])
        out_dir = dst / sid
        out_dir.mkdir(exist_ok=True)
        entry = {"scenario_id": sid, "online_success": orig.get("success"),
                 "online_fix_case_bug": orig.get("judge_fix_case_bug")}
        for tag, fix in (("orig", False), ("fixed", True)):
            r = rejudge_one(row, trace, cfg, fix)
            (out_dir / f"rejudge_{tag}.json").write_text(
                json.dumps(r, ensure_ascii=False, indent=1, default=str))
            entry[f"offline_{tag}_success"] = r["success"]
            entry[f"offline_{tag}_rationale"] = (r["rationale"] or "")[:400]
            print(f"[{tag}] {sid} success={r['success']} turns_in_trace={r['agent_turns_in_trace']}/{r['nb_turns']}",
                  flush=True)
            for jl in r["judge_log"]:
                print(f"    {jl['checker']}: verdict={jl['verdict']} :: {jl['evaluation_tail'][-80:]!r}", flush=True)
        summary.append(entry)
    (dst / "rejudge_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    import csv
    with open(dst / "summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        # online = the run's own (online) judge result, scored with online_fix_case_bug
        w.writerow(["scenario_id", "online", "online_fix_case_bug", "offline_original", "offline_fixed",
                    "fixed_rationale"])
        for e in summary:
            w.writerow([e["scenario_id"], e["online_success"], e["online_fix_case_bug"], e["offline_orig_success"],
                        e["offline_fixed_success"], e["offline_fixed_rationale"].replace("\n", " ")])


if __name__ == "__main__":
    main()
