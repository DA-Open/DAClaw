"""Run DAClaw's real AIAgent on official PersonaMem-v3 ranking instances.

Uses the upstream prompt, time-masked snapshot, and deterministic scorer.
Credentials are read from PM3_API_KEY, never serialized. Each query runs in a
fresh process with only scoped, read-only DAClaw history tools available.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
DACLAW = HERE.parents[2]
FILES = ("instagram.json", "facebook.json", "threads.json", "chatbot.json",
         "ai_studio.json", "calendar.json", "test.json")


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    temporary.replace(path)


def endpoint_base(value):
    value = value.rstrip("/")
    suffix = "/chat/completions"
    return value[:-len(suffix)] if value.endswith(suffix) else value


def scoped_arguments(name, arguments, snapshot):
    """Enforce the benchmark's GT/time firewall at the actual tool boundary."""
    if name not in {"read_file", "search_files"}:
        raise ValueError("Only read_file and search_files are available")
    args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
    path = Path(args.get("path", ".")).expanduser()
    path = (snapshot / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(snapshot):
        raise ValueError("History access is restricted to the time-masked snapshot")
    args["path"] = str(path)
    # rg accepts '-' as a special stdin path; use the validated absolute path.
    return args


def worker(request_file):
    request = json.loads(Path(request_file).read_text())
    work = Path(request_file).resolve().parent
    snapshot = Path(request["snapshot"]).resolve()
    profile = work / "daclaw_home"
    profile.mkdir(exist_ok=True)
    (profile / "config.yaml").write_text(
        "compression:\n  enabled: false\nmemory:\n  memory_enabled: false\n"
        "  user_profile_enabled: false\n")
    os.environ.update(DACLAW_HOME=str(profile), TERMINAL_ENV="local",
                      TERMINAL_CWD=str(snapshot), DACLAW_EXEC_AUDIT_ENABLED="0")
    key = os.environ.pop("PM3_API_KEY", None)
    if not key:
        raise ValueError("Set PM3_API_KEY in the environment")
    os.chdir(snapshot)
    sys.path.insert(0, str(DACLAW))
    import run_agent

    original_dispatch = run_agent.handle_function_call

    def history_dispatch(function_name, function_args, *args, **kwargs):
        try:
            scoped = scoped_arguments(function_name, function_args, snapshot)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return json.dumps({"error": str(exc)})
        return original_dispatch(function_name, scoped, *args, **kwargs)

    run_agent.handle_function_call = history_dispatch
    cfg = request["config"]

    def event(kind, **payload):
        with (work / "events.jsonl").open("a") as handle:
            handle.write(json.dumps({"timestamp": time.time(), "event": kind,
                                     **payload}, default=str) + "\n")

    agent = run_agent.AIAgent(
        model=cfg["model"], base_url=cfg["base_url"], api_key=key,
        provider="custom", api_mode="chat_completions",
        client_options={"timeout": 120, "max_retries": 2},
        max_iterations=cfg["max_iterations"], max_tokens=4096, tool_delay=0,
        enabled_toolsets=["file"], quiet_mode=True, skip_context_files=True,
        skip_memory=True, persist_session=False,
        step_callback=lambda iteration, previous: event("iteration", iteration=iteration),
        tool_start_callback=lambda tid, name, args: event(
            "tool_start", tool_call_id=tid, name=name, arguments=args),
        tool_complete_callback=lambda tid, name, args, result: event(
            "tool_complete", tool_call_id=tid, name=name, result=result),
    )
    # Set the toolset once BEFORE the conversation; never mutate past context.
    agent.tools = [t for t in agent.tools if t["function"]["name"] in
                   {"read_file", "search_files"}]
    agent.valid_tool_names = {t["function"]["name"] for t in agent.tools}
    if agent.valid_tool_names != {"read_file", "search_files"}:
        raise RuntimeError("DAClaw history tools were not loaded")
    result = agent.run_conversation(
        request["prompt"], task_id="pm3-" + request["query_id"],
        system_message=(
            "You are an assistant acting on behalf of this user. Their cross-app "
            "interaction history is in JSON files in your current directory. "
            "Read README.md first with read_file. Use search_files and narrow "
            "read_file ranges to gather relevant evidence across the apps. "
            "The snapshot is time-masked to before the query. Infer preferences "
            "from the user's interactions. Follow the task's output schema. "
            "All history tools are read-only and restricted to this snapshot."
        ),
    )
    # Defense in depth: do not persist the supplied credential even on errors.
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    dump(work / "trajectory.json", json.loads(serialized.replace(key, "[REDACTED]")))


def fetch_backend(args, backend):
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
    provenance = []
    for filename in FILES:
        target = backend / args.user_id / filename
        url = (args.data_endpoint.rstrip("/") + "/datasets/bowen-upenn/PersonaMem-v3/"
               "resolve/main/backend/" + args.user_id + "/" + filename)
        if not target.exists():
            print("Downloading", filename, flush=True)
            response = session.get(url, timeout=(15, 120))
            response.raise_for_status()
            value = response.json()
            dump(target, value)
        provenance.append({"file": filename, "url": url,
                           "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    return provenance


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "summary.json").exists():
        raise FileExistsError("Output already has summary.json; choose a fresh --output")
    sys.path.insert(0, str(args.benchmark.resolve()))
    from evaluation.backend_query import BackendQuery, materialize_snapshot
    from evaluation.tasks.personalized_recommendation import (
        personalized_recommendation_prompt, compute_personalized_recommendation_metrics,
    )
    from data_preparation.utils import extract_json_from_response

    config = {"model": args.model, "base_url": endpoint_base(args.base_url),
              "max_iterations": args.max_iterations,
              "benchmark": str(args.benchmark.resolve()), "user_id": args.user_id,
              "limit": args.limit, "task_type": "personalized_recommendation",
              "selection": "first N matching official test.json instances, in file order",
              "scorer": "upstream compute_personalized_recommendation_metrics",
              "judge_model": None, "harness": "DAClaw run_agent.AIAgent",
              "tools": ["read_file", "search_files"], "api_key_env": "PM3_API_KEY"}
    dump(output / "run_config.json", config)
    backend = args.backend_dir.resolve() if args.backend_dir else output / "data" / "backend"
    provenance = fetch_backend(args, backend) if args.download else []
    test_path = backend / args.user_id / "test.json"
    items = json.loads(test_path.read_text())
    selected = [(i, item) for i, item in enumerate(items)
                if item.get("task_type") == "personalized_recommendation"][:args.limit]
    if len(selected) != args.limit:
        raise ValueError(f"Need {args.limit} official instances, found {len(selected)}")
    dump(output / "data_provenance.json", provenance)
    dump(output / "selected_tasks.json", [item for _, item in selected])
    bq = BackendQuery(backend)
    rows = []
    for number, (seq, item) in enumerate(selected, 1):
        query_id = item["query_id"]
        inst = dict(item.get("instance_full") or item)
        if item.get("user_query") and "user_query" not in inst:
            inst["user_query"] = item["user_query"]
        work = output / "tasks" / f"{number:03d}"
        work.mkdir(parents=True, exist_ok=True)
        snapshot = materialize_snapshot(bq, args.user_id, int(inst["t_test"]),
                                        out_dir=work / "history")
        prompt = personalized_recommendation_prompt(inst, None)
        request = {"query_id": query_id, "snapshot": str(snapshot),
                   "prompt": prompt, "config": config}
        dump(work / "request.json", request)
        (work / "prompt.txt").write_text(prompt)
        started = time.monotonic()
        row = {"query_id": query_id, "seq": seq, "user_id": args.user_id,
               "task_type": item["task_type"], "status": "error"}
        print(f"[{number}/{args.limit}] {query_id}", flush=True)
        try:
            with (work / "agent.log").open("w") as log:
                proc = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                       "_worker", str(work / "request.json")],
                                      stdout=log, stderr=subprocess.STDOUT,
                                      timeout=args.timeout)
            if proc.returncode:
                raise RuntimeError(f"Agent exited {proc.returncode}; see tasks/{number:03d}/agent.log")
            trajectory = json.loads((work / "trajectory.json").read_text())
            answer = trajectory.get("final_response") or ""
            parsed = extract_json_from_response(answer)
            if not isinstance(parsed, dict):
                raise ValueError("Agent did not return a JSON object")
            ranked = parsed.get("ranked_indexes")
            n = len(inst["candidates"])
            if (not isinstance(ranked, list) or any(type(x) is not int for x in ranked)
                    or len(ranked) != n or set(ranked) != set(range(n))):
                raise ValueError("Agent ranking is not a complete candidate permutation")
            if not trajectory.get("completed") or trajectory.get("interrupted"):
                raise RuntimeError("Agent did not complete normally")
            row.update(status="ok", metrics=compute_personalized_recommendation_metrics(parsed, inst),
                       agent_response=answer, api_calls=trajectory.get("api_calls"),
                       tool_calls=sum(len(m.get("tool_calls") or [])
                                      for m in trajectory.get("messages", [])))
        except Exception as exc:
            row["error"] = str(exc)
        row["duration_ms"] = round((time.monotonic() - started) * 1000)
        dump(work / "result.json", row)
        rows.append(row)
        dump(output / "results.json", rows)
        print(row["status"], row.get("metrics", row.get("error")), flush=True)

    successful = [r for r in rows if r["status"] == "ok"]
    averages = {}
    if successful:
        for key in successful[0]["metrics"]:
            values = [r["metrics"].get(key) for r in successful]
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
                averages[key] = sum(values) / len(values)
    summary = {"model": args.model, "harness": "DAClaw", "requested": args.limit,
               "attempted": len(rows), "succeeded": len(successful),
               "failed": len(rows) - len(successful), "mean_metrics": averages,
               "headline_metric": "tier_concordance",
               "scope": "3-instance smoke run of personalized_recommendation, not the full benchmark",
               "judge": "none; official deterministic ranking metrics"}
    dump(output / "summary.json", summary)
    with (output / "results.csv").open("w", newline="") as handle:
        fields = ["query_id", "seq", "user_id", "task_type", "status", "duration_ms",
                  "api_calls", "tool_calls", "metrics_json", "error", "agent_response"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            record = dict(row)
            record["metrics_json"] = json.dumps(record.pop("metrics", {}))
            writer.writerow(record)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if len(successful) == args.limit else 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_worker":
        worker(sys.argv[2])
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, default=DACLAW.parent / "PersonaMem-v3")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend-dir", type=Path)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--data-endpoint", default="https://huggingface.co")
    parser.add_argument("--user-id", default="8")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--base-url", default="https://api2.aigcbest.top/v1")
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if not os.environ.get("PM3_API_KEY"):
        parser.error("Set PM3_API_KEY in the environment")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
