"""GAIA2 runner for DAClaw harness ③.

  cd ~/DAClaw
  .venv/bin/python -m environments.benchmarks.gaia2.bench sample  --config environments/benchmarks/gaia2/default.yaml
  .venv/bin/python -m environments.benchmarks.gaia2.bench run     --config ... --name <run_name> [--ids a,b]
  .venv/bin/python -m environments.benchmarks.gaia2.bench summary --config ... --name <run_name>

Outputs go to <bench_root>/runs/<run_name>/:
  config.json                 resolved config + sampled scenario ids (no secrets)
  <scenario_id>/result.json   pass/fail, judge rationale, turns, tokens, timings, errors
  <scenario_id>/messages.json full agent conversation (OpenAI format)
  <scenario_id>/run.log       log of that scenario
  <scenario_id>/are_trace/    ARE-format trace export
  summary.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def _x(p: str) -> str:
    return os.path.expanduser(p)


def load_cfg(path: str) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    cfg["paths"] = {k: _x(v) if isinstance(v, str) else v for k, v in cfg["paths"].items()}
    return cfg


def setup_env(cfg: dict) -> None:
    p = cfg["paths"]
    os.environ["DEMO_FS_PATH"] = p["demo_fs_path"]
    os.environ["HF_HOME"] = p["hf_home"]
    os.environ.setdefault("HF_ENDPOINT", p["hf_endpoint"])
    key_name = cfg["agent"]["api_key_env"]
    if not os.environ.get(key_name):
        for line in Path(p["daclaw_env_file"]).read_text().splitlines():
            if line.startswith(key_name + "="):
                os.environ[key_name] = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not os.environ.get(key_name):
        sys.exit(f"{key_name} not found")
    from environments.benchmarks.gaia2.are_bridge import ensure_are_importable
    ensure_are_importable(p["are_site"])


def load_rows(cfg: dict):
    from datasets import load_dataset
    d = cfg["dataset"]
    return load_dataset(d["hf_repo"], d["hf_config"], split=d["split"])


def pick(cfg: dict, ds) -> list:
    d = cfg["dataset"]
    if d.get("scenario_ids"):
        want = list(d["scenario_ids"])
        idx = {sid: i for i, sid in enumerate(ds["scenario_id"])}
        return [idx[s] for s in want]
    return sorted(random.Random(d["sample_seed"]).sample(range(len(ds)), d["sample_size"]))


def cmd_sample(args):
    cfg = load_cfg(args.config)
    setup_env(cfg)
    ds = load_rows(cfg)
    for i in pick(cfg, ds):
        print(i, ds[i]["scenario_id"], ds[i].get("category"))


def _scenario_logger(path: Path) -> logging.Handler:
    h = logging.FileHandler(path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(h)
    return h


async def _run_one(row: dict, cfg: dict, sd: Path) -> dict:
    """One scenario in its own event loop, with its own client.

    Each scenario runs under a fresh asyncio.run(); httpx's async connection
    pool must not cross event loops, and the client is closed inside the loop
    so no close task is left for a loop that is already gone.
    """
    from openai import AsyncOpenAI
    from environments.benchmarks.gaia2.run_scenario import run_scenario

    client = AsyncOpenAI(base_url=cfg["agent"]["base_url"],
                         api_key=os.environ[cfg["agent"]["api_key_env"]], timeout=600)
    try:
        return await asyncio.wait_for(run_scenario(row, cfg, sd, client),
                                      timeout=cfg["run"]["scenario_timeout"])
    finally:
        await client.close()


def cmd_run(args):
    cfg = load_cfg(args.config)
    setup_env(cfg)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "openai", "LiteLLM", "litellm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    rd = Path(cfg["paths"]["bench_root"]) / "runs" / args.name
    rd.mkdir(parents=True, exist_ok=True)
    ds = load_rows(cfg)
    idxs = pick(cfg, ds)
    if args.ids:
        want = set(args.ids.split(","))
        idxs = [i for i in idxs if ds[i]["scenario_id"] in want]
    picked = [{"index": i, "scenario_id": ds[i]["scenario_id"], "category": ds[i].get("category")} for i in idxs]
    cfg_out = json.loads(json.dumps(cfg))
    # Record the ids actually run so config.json is self-consistent.
    cfg_out["dataset"]["scenario_ids"] = [p["scenario_id"] for p in picked]
    cfg_out["sampled"] = picked
    cfg_out["run_name"] = args.name
    cfg_out["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (rd / "config.json").write_text(json.dumps(cfg_out, ensure_ascii=False, indent=1))

    for p in picked:
        sd = rd / p["scenario_id"]
        res_path = sd / "result.json"
        if res_path.exists() and not args.force:
            prev = json.loads(res_path.read_text())
            if prev.get("status", "ok") == "ok":
                print(f"[skip] {p['scenario_id']} (done)")
                continue
            print(f"[rerun] {p['scenario_id']} (previous status={prev.get('status')})", flush=True)
        sd.mkdir(parents=True, exist_ok=True)
        h = _scenario_logger(sd / "run.log")
        print(f"[start] {p['scenario_id']} ({p['category']})", flush=True)
        try:
            row = dict(ds[p["index"]])
            res = asyncio.run(_run_one(row, cfg, sd))
        except asyncio.TimeoutError:
            res = {"scenario_id": p["scenario_id"], "category": p["category"], "status": "timeout",
                   "success": None, "error": "scenario_timeout"}
            res_path.write_text(json.dumps(res, indent=1))
        finally:
            logging.getLogger().removeHandler(h)
            h.close()
        print(f"[done] {p['scenario_id']} status={res.get('status')} success={res.get('success')} "
              f"{res.get('elapsed_seconds')}s :: {str(res.get('rationale') or res.get('error'))[:200]}", flush=True)
    write_summary(rd)


def fix_paths(rd: Path) -> None:
    """Rewrite absolute paths in result.json after a run dir is moved/renamed."""
    for f in rd.glob("*/result.json"):
        r = json.loads(f.read_text())
        sd = f.parent
        r["messages_path"] = str(sd / "messages.json")
        traces = sorted((sd / "are_trace").glob("*.json"))
        if traces:
            r["are_trace_path"] = str(traces[0])
        f.write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str))


def write_summary(rd: Path):
    fix_paths(rd)
    rows = []
    for f in sorted(rd.glob("*/result.json")):
        r = json.loads(f.read_text())
        u = r.get("usage") or {}
        rows.append({
            "scenario_id": r.get("scenario_id"), "category": r.get("category"),
            # result.json files written before `status` existed were all scored runs
            "status": r.get("status", "ok"),
            "success": r.get("success"), "nb_turns": r.get("nb_turns"),
            "llm_calls": r.get("llm_calls"), "tool_calls": r.get("tool_calls"),
            "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
            "elapsed_seconds": r.get("elapsed_seconds"), "error": r.get("error"),
            "rationale": (r.get("rationale") or "").replace("\n", " ")[:500],
        })
    if not rows:
        return
    with open(rd / "summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    scored = [r for r in rows if r["status"] == "ok"]
    n_pass = sum(1 for r in scored if r["success"] is True)
    n_bad = len(rows) - len(scored)
    print(f"summary: {n_pass}/{len(scored)} passed"
          + (f", {n_bad} not scored (error/timeout; rerun with the same --name)" if n_bad else "")
          + f" -> {rd / 'summary.csv'}")


def cmd_summary(args):
    cfg = load_cfg(args.config)
    write_summary(Path(cfg["paths"]["bench_root"]) / "runs" / args.name)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sample", "run", "summary"):
        s = sub.add_parser(name)
        s.add_argument("--config", default=str(HERE / "default.yaml"))
        if name in ("run", "summary"):
            s.add_argument("--name", required=True)
        if name == "run":
            s.add_argument("--ids", default="")
            s.add_argument("--force", action="store_true")
    args = ap.parse_args()
    {"sample": cmd_sample, "run": cmd_run, "summary": cmd_summary}[args.cmd](args)


if __name__ == "__main__":
    main()
