"""DAComp-DA evaluation interface for DAClaw (host-side orchestrator, stdlib only).

Each task attempt runs in a fresh `daclaw` Docker container (see run_task.py), and the
submissions are scored with the official DAComp llm_judge.py / get_score.py.

  python bench.py sample  --n 10 --seed 42 --out tasks_rand10.txt
  python bench.py run     --run-name NAME --tasks tasks_rand10.txt [--workers 3]
  python bench.py judge   --run-name NAME [--judge-model gpt-6.1-sol]
  python bench.py summary --run-name NAME

Layout under --root (default ~/dacomp_bench):
  data/dacomp-da.jsonl, data/<id>/<id>.sqlite      cached task data (HF, via hf-mirror)
  DAComp/                                          official repo (evaluation_suite, rubrics)
  runs/<run>/config.json                           frozen run config
  runs/<run>/state.json                            per-task status + every attempt
  runs/<run>/attempts/<id>/<n>/{out,meta,home,work}
  runs/<run>/agent_results/<run>/<id>/             official submission layout
  runs/<run>/scores/                               judge CSVs + overall_results.csv
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_task  # noqa: E402  # only for trajectory formatting; DAClaw imports stay inside its main()
HF_DATASET = "DAComp/dacomp-da"
FINAL_STATUSES = {"ok", "incomplete", "no_answer", "timeout"}  # agent outcomes, never retried
RETRYABLE = {"api_error", "error", "container_error"}  # infrastructure failures
IMAGE_REF = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_state_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------------------- data

def hf_url(path: str) -> str:
    endpoint = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
    return f"{endpoint}/datasets/{HF_DATASET}/resolve/main/{path}"


def download(url: str, dest: Path, timeout: int = 600, attempts: int = 5) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"})  # hf-mirror 403s urllib's UA
    for i in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            tmp.replace(dest)
            return
        except Exception as exc:  # the mirror returns transient 5xx under load
            if i == attempts:
                raise
            log(f"download failed ({exc}); retry {i}/{attempts - 1} in {15 * i}s")
            time.sleep(15 * i)


def load_tasks(root: Path) -> dict:
    jsonl = root / "data" / "dacomp-da.jsonl"
    if not jsonl.is_file():
        log("downloading dacomp-da.jsonl")
        download(hf_url("dacomp-da.jsonl"), jsonl)
    tasks = {}
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            tasks[row["instance_id"]] = row
    return tasks


def ensure_sqlite(root: Path, iid: str) -> Path:
    db = root / "data" / iid / f"{iid}.sqlite"
    if not db.is_file():
        log(f"downloading {iid}.sqlite")
        download(hf_url(f"{iid}/{iid}.sqlite"), db)
    return db


def read_task_list(path: str) -> list:
    ids = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    return ids


# ----------------------------------------------------------------------------- state

def run_dir(root: Path, name: str) -> Path:
    return root / "runs" / name


def load_state(rd: Path) -> dict:
    p = rd / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"tasks": {}}


def save_state(rd: Path, state: dict) -> None:
    p = rd / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def submission_complete(sub: Path, iid: str) -> bool:
    md, traj = sub / f"{iid}.md", sub / f"{iid}-traj.txt"
    if not (md.is_file() and traj.is_file()):
        return False
    refs = IMAGE_REF.findall(md.read_text(encoding="utf-8", errors="replace"))
    return all((sub / r).is_file() for r in refs)


# ----------------------------------------------------------------------------- docker

def image_revision(image: str) -> str:
    try:
        out = subprocess.run(
            ["docker", "inspect", image, "--format",
             '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'],
            capture_output=True, text=True, timeout=60, check=True).stdout.strip()
        return out
    except Exception as exc:
        sys.exit(f"cannot inspect image {image}: {exc}")


def make_home(home: Path, daclaw_home: Path, env_keys: list) -> None:
    """Fresh DACLAW_HOME per attempt: model config + only the env keys the agent needs."""
    home.mkdir(parents=True, exist_ok=True)
    shutil.copy2(daclaw_home / "config.yaml", home / "config.yaml")
    env_lines = []
    src_env = daclaw_home / ".env"
    if src_env.is_file():
        for line in src_env.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip()
            if key in env_keys:
                env_lines.append(line)
    (home / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    os.chmod(home / ".env", 0o600)
    os.chmod(home / "config.yaml", 0o600)


def run_attempt(cfg: dict, rd: Path, task: dict, attempt: int) -> dict:
    iid = task["instance_id"]
    root = Path(cfg["root"])
    adir = rd / "attempts" / iid / str(attempt)
    if adir.exists():
        shutil.rmtree(adir)
    out, meta, home, work = (adir / x for x in ("out", "meta", "home", "work"))
    for d in (out, meta, work / "home"):
        d.mkdir(parents=True, exist_ok=True)
    make_home(home, Path(cfg["daclaw_home"]), cfg["env_keys"])
    (meta / "task.json").write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    db = ensure_sqlite(root, iid)

    name = f"dacomp-{cfg['run_name']}-{iid}-{attempt}".lower()
    name = re.sub(r"[^a-z0-9_.-]", "-", name)[:120]
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--memory", cfg["memory"], "--cpus", str(cfg["cpus"]), "--pids-limit", "1024",
        "--entrypoint", "/opt/daclaw/.venv/bin/python",
        "-e", "DACLAW_HOME=/opt/data", "-e", "HOME=/work/home",
        "-e", "DACLAW_WEB_WORKSPACE_ROOT=/work", "-w", "/work",
        "-v", f"{work}:/work", "-v", f"{db.parent}:/data:ro",
        "-v", f"{home}:/opt/data", "-v", f"{out}:/out", "-v", f"{meta}:/meta",
        "-v", f"{HERE}:/bench:ro",
        cfg["image"], "/bench/run_task.py",
        "--model", cfg["model"], "--max-iterations", str(cfg["max_iterations"]),
    ]
    if cfg.get("temperature") is not None:
        cmd += ["--temperature", str(cfg["temperature"])]

    t0 = time.time()
    rec = {"attempt": attempt, "started": datetime.now().isoformat(timespec="seconds"), "container": name}
    with open(adir / "container.log", "w", encoding="utf-8") as logf:
        try:
            proc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT, timeout=cfg["timeout"])
            rec["exit_code"] = proc.returncode
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            rec["exit_code"] = None
            rec["status"] = "timeout"
    rec["wall_s"] = round(time.time() - t0, 1)

    meta_file = meta / "meta.json"
    if meta_file.is_file():
        m = json.loads(meta_file.read_text(encoding="utf-8"))
        status = m.get("status", "error")
        rec.setdefault("status", "container_error" if status == "running" else status)
        for k in ("model", "api_calls", "total_tokens", "input_tokens", "output_tokens",
                  "estimated_cost_usd", "agent_error", "missing_image_refs", "fence_stripped",
                  "tool_errors"):
            if m.get(k) is not None:
                rec[k] = m[k]
        rec["figures_ok"] = sum(1 for f in m.get("figures", []) if f.get("ok"))
        rec["figures_failed"] = sum(1 for f in m.get("figures", []) if not f.get("ok"))
    else:
        rec.setdefault("status", "container_error")
    # Killed before run_task.py finished (timeout / crash): recover the partial transcript that
    # DAClaw saves after every turn, so the trajectory and tool errors are not lost.
    if not (out / f"{iid}-traj.txt").is_file():
        messages = recover_session_messages(home)
        rec["recovered_messages"] = len(messages)
        if messages:
            (meta / "messages.json").write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
        (out / f"{iid}-traj.txt").write_text(
            run_task.format_trajectory(messages, "") if messages
            else f"(Run status: {rec['status']}. No transcript was saved.)\n", encoding="utf-8")
        rec["api_calls"] = sum(1 for m in messages if m.get("role") == "assistant")
        rec["tool_errors"] = run_task.tool_errors(messages)
    if not (out / f"{iid}.md").is_file():
        # The judge still sees the task and scores it as failed.
        (out / f"{iid}.md").write_text(f"(No answer produced. Run status: {rec['status']}.)\n", encoding="utf-8")
    return rec


def recover_session_messages(home: Path) -> list:
    logs = sorted((home / "sessions").glob("session_*.json"), key=lambda p: p.stat().st_size, reverse=True)
    for path in logs:
        try:
            msgs = json.loads(path.read_text(encoding="utf-8")).get("messages") or []
        except Exception:
            continue
        if msgs:
            return msgs
    return []


def process_task(cfg: dict, rd: Path, state: dict, task: dict) -> None:
    iid = task["instance_id"]
    entry = state["tasks"].setdefault(iid, {"attempts": []})
    while True:
        attempt = len(entry["attempts"]) + 1
        log(f"{iid}: attempt {attempt} starting")
        rec = run_attempt(cfg, rd, task, attempt)
        with _state_lock:
            entry["attempts"].append(rec)
            entry["status"] = rec["status"]
            entry["final_attempt"] = attempt
            save_state(rd, state)
        log(f"{iid}: attempt {attempt} -> {rec['status']} ({rec['wall_s']}s, "
            f"calls={rec.get('api_calls')}, figs={rec.get('figures_ok', 0)})")
        if rec["status"] not in RETRYABLE or attempt > cfg["max_retries"]:
            break
        time.sleep(10 * attempt)

    sub = rd / "agent_results" / cfg["run_name"] / iid
    if sub.exists():
        shutil.rmtree(sub)
    src = rd / "attempts" / iid / str(entry["final_attempt"]) / "out"
    if src.is_dir() and any(src.iterdir()):
        shutil.copytree(src, sub)
    with _state_lock:
        entry["submission_complete"] = submission_complete(sub, iid)
        save_state(rd, state)


def cmd_run(args) -> None:
    root = Path(args.root).expanduser()
    rd = run_dir(root, args.run_name)
    rd.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock_fh = open(rd / ".lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one orchestrator per run
    except BlockingIOError:
        sys.exit(f"run {args.run_name} is already being executed by another process")
    all_tasks = load_tasks(root)
    ids = read_task_list(args.tasks)
    unknown = [i for i in ids if i not in all_tasks]
    if unknown:
        sys.exit(f"unknown instance ids: {unknown}")

    cfg_path = rd / "config.json"
    cfg = {
        "run_name": args.run_name, "root": str(root), "image": args.image,
        "image_id_revision": image_revision(args.image), "model": args.model,
        "max_iterations": args.max_iterations, "temperature": args.temperature,
        "timeout": args.timeout, "max_retries": args.max_retries, "workers": args.workers,
        "memory": args.memory, "cpus": args.cpus, "daclaw_home": str(Path(args.daclaw_home).expanduser()),
        "env_keys": args.env_keys.split(","), "tasks": ids, "language": "en",
    }
    if cfg_path.is_file():
        old = json.loads(cfg_path.read_text(encoding="utf-8"))
        frozen = ("image_id_revision", "model", "max_iterations", "temperature", "timeout")
        changed = {k: (old.get(k), cfg[k]) for k in frozen if old.get(k) != cfg[k]}
        if changed and not args.force_config:
            sys.exit(f"run config differs from the saved one {changed}; use a new --run-name "
                     "or pass --force-config")
        cfg["tasks"] = list(dict.fromkeys(old.get("tasks", []) + ids))
        cfg["created"] = old.get("created")
    cfg.setdefault("created", datetime.now().isoformat(timespec="seconds"))
    from_meta = HERE / "run_task.py"
    cfg["run_task_version"] = re.search(r'PROMPT_VERSION = "([^"]+)"', from_meta.read_text()).group(1)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    for iid in ids:  # download before starting containers
        ensure_sqlite(root, iid)
    state = load_state(rd)
    todo = []
    for iid in ids:
        entry = state["tasks"].get(iid)
        sub = rd / "agent_results" / args.run_name / iid
        if entry and entry.get("status") in FINAL_STATUSES and submission_complete(sub, iid):
            log(f"{iid}: already done ({entry['status']}), skipping")
            continue
        todo.append(all_tasks[iid])
    log(f"run {args.run_name}: {len(todo)} to run, {len(ids) - len(todo)} done, workers={args.workers}")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_task, cfg, rd, state, t): t["instance_id"] for t in todo}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                log(f"{futures[fut]}: orchestrator error {exc!r}")
    cmd_summary(args)


# ----------------------------------------------------------------------------- judge

JUDGE_WRAPPER = r'''
import json, os, sys, runpy
suite = sys.argv[1]
spec = json.loads(os.environ["DACOMP_JUDGE_SPEC"])
sys.path.insert(0, suite)
os.chdir(suite)
import requests
_post = requests.post
class _Resp:  # minimal stand-in for requests.Response as used by core/llm_client.py
    def __init__(self, status_code, payload, text=""):
        self.status_code, self._payload, self.text = status_code, payload, text or json.dumps(payload)
    def json(self):
        return self._payload


def _post_stream(url, headers=None, json=None, timeout=None, **kw):
    """Stream the completion so the relay's ~100s gateway timeout (HTTP 524) never
    fires while a reasoning judge is still thinking; reassemble a normal response."""
    import json as _json
    body = dict(json or {}, stream=True)
    try:
        r = _post(url, headers=headers, json=body, timeout=(30, spec["timeout"]), stream=True, **kw)
    except requests.RequestException:
        raise
    if r.status_code != 200 or "text/event-stream" not in r.headers.get("content-type", ""):
        try:
            return _Resp(r.status_code, r.json())
        except ValueError:
            return _Resp(r.status_code, {}, r.text[:500])
    parts, finish = [], None
    for raw in r.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = _json.loads(data)
        except ValueError:
            continue
        for ch in chunk.get("choices") or []:
            parts.append((ch.get("delta") or {}).get("content") or "")
            finish = ch.get("finish_reason") or finish
    content = "".join(parts)
    if not content:
        return _Resp(502, {"error": {"code": "empty_stream", "message": "stream returned no content"}})
    return _Resp(200, {"choices": [{"message": {"role": "assistant", "content": content},
                                    "finish_reason": finish}]})


requests.post = _post_stream
import core.config as C
C.model_config[spec["name"]] = {
    "provider": "http", "model_name": spec["model"],
    "base_url": spec["base_url"].rstrip("/") + "/chat/completions",
    "api_key": os.environ["DACOMP_JUDGE_API_KEY"],
    "generate_kwargs": dict({"max_tokens": spec["max_tokens"], "temperature": 0},
                            **({"reasoning_effort": spec["reasoning_effort"]} if spec.get("reasoning_effort") else {})),
}
C.VISION_CAPABLE_MODEL_CONFIGS.add(spec["name"])
sys.argv = ["llm_judge.py"] + sys.argv[2:]
runpy.run_path("llm_judge.py", run_name="__main__")
'''


def read_env_key(daclaw_home: Path, key: str) -> str:
    for line in (daclaw_home / ".env").read_text(encoding="utf-8").splitlines():
        if line.split("=", 1)[0].strip() == key:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(f"{key} not found in {daclaw_home / '.env'}")


def cmd_judge(args) -> None:
    root = Path(args.root).expanduser()
    rd = run_dir(root, args.run_name)
    suite = root / "DAComp" / "dacomp-da" / "evaluation_suite"
    subs = rd / "agent_results" / args.run_name
    if not subs.is_dir():
        sys.exit(f"no submissions at {subs}")
    scores = rd / "scores"
    scores.mkdir(exist_ok=True)
    spec = {"name": f"judge-{args.judge_model}", "model": args.judge_model, "base_url": args.judge_base_url,
            "max_tokens": args.judge_max_tokens, "timeout": args.judge_timeout,
            "reasoning_effort": args.judge_reasoning_effort}
    env = dict(os.environ, DACOMP_JUDGE_SPEC=json.dumps(spec),
               DACOMP_JUDGE_API_KEY=read_env_key(Path(args.daclaw_home).expanduser(), args.judge_key_env))
    wrapper = rd / "judge_wrapper.py"
    wrapper.write_text(JUDGE_WRAPPER, encoding="utf-8")
    cmd = [args.judge_python, str(wrapper), str(suite),
           "--rubrics-model", spec["name"], "--gsb-model-text", spec["name"], "--gsb-model-vis", spec["name"],
           "--inputs", str(subs), "--output-dir", str(scores), "--language", "en",
           "--max-workers", str(args.judge_workers)]
    (rd / "judge_config.json").write_text(json.dumps(
        dict(spec, started=datetime.now().isoformat(timespec="seconds"),
             dacomp_revision=(suite.parent / "REVISION").read_text().strip()
             if (suite.parent / "REVISION").is_file() else None),
        indent=2), encoding="utf-8")
    log("running official llm_judge.py")
    with open(rd / "judge.log", "w", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        sys.exit(f"llm_judge failed (exit {rc}), see {rd / 'judge.log'}")
    log("running official get_score.py")
    with open(rd / "judge.log", "a", encoding="utf-8") as fh:
        rc = subprocess.run([args.judge_python, "get_score.py", "--scores-dir", str(scores)],
                            cwd=suite, stdout=fh, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        sys.exit(f"get_score failed (exit {rc}), see {rd / 'judge.log'}")
    cmd_summary(args)


# ----------------------------------------------------------------------------- summary

def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, 2)  # NaN -> None


def _pct(value):
    """GSB scores are 0-1 in the per-task CSV; report them on the same 0-100 scale as overall_results."""
    v = _num(value)
    return None if v is None else round(v * 100, 2)


def write_result_jsons(rd: Path, cfg: dict, state: dict, scores: dict) -> None:
    """One <id>.json per task, field-compatible with the group's GDPval per-task output."""
    out_dir = rd / "results"
    out_dir.mkdir(exist_ok=True)
    tasks = load_tasks(Path(cfg["root"]))
    for iid in cfg["tasks"]:
        entry = state["tasks"].get(iid)
        if not entry or not entry.get("attempts"):
            continue
        last = entry["attempts"][-1]
        adir = rd / "attempts" / iid / str(entry["final_attempt"])
        meta_path, msgs_path = adir / "meta" / "meta.json", adir / "meta" / "messages.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        messages = json.loads(msgs_path.read_text(encoding="utf-8")) if msgs_path.is_file() else []
        s = scores.get(iid)
        grade = None
        if s:
            grade = {
                "completeness": _num(s.get("rubric_completeness_score_pct")),
                "accuracy": _num(s.get("rubric_accuracy_score_pct")),
                "conclusiveness": _num(s.get("rubric_conclusiveness_score_pct")),
                "readability": _pct(s.get("gsb_readability_score")),
                "professionalism": _pct(s.get("gsb_professionalism_score")),
                "visualization": _pct(s.get("gsb_visualization_score")),
                "rubric_points": _num(s.get("rubrics_total_score")),
                "rubric_points_possible": _num(s.get("full_score")),
                "rubrics_result": s.get("rubrics_result"),
                "gsb_text_results": s.get("gsb_text_results") or s.get("gsb_results"),
                "gsb_vis_results": s.get("gsb_vis_results"),
            }
        score = _num(s.get("weighted_total_score")) if s else None
        status = entry.get("status")
        result = {
            "task_id": iid,
            "benchmark": "dacomp-da",
            "instruction": tasks.get(iid, {}).get("instruction"),
            "workspace": str(rd / "agent_results" / cfg["run_name"] / iid),
            "model": last.get("model") or cfg.get("model"),
            "turns_used": last.get("api_calls"),
            "finished_naturally": status == "ok",
            "status": status,
            "attempts": len(entry["attempts"]),
            "messages": messages,
            "tool_errors": last.get("tool_errors") or meta.get("tool_errors") or [],
            "figures": meta.get("figures", []),
            "usage": {k: last.get(k) for k in ("input_tokens", "output_tokens", "total_tokens",
                                               "estimated_cost_usd")},
            "grade": grade,
            "score": score,
            "reward": round(score / 100, 4) if score is not None else None,
            "passed": None,  # DAComp-DA has no pass/fail threshold
            "error": last.get("agent_error") or (None if status == "ok" else status),
            "elapsed_seconds": last.get("wall_s"),
        }
        (out_dir / f"{iid}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                             encoding="utf-8")


def cmd_summary(args) -> None:
    import csv

    root = Path(args.root).expanduser()
    rd = run_dir(root, args.run_name)
    state = load_state(rd)
    cfg = json.loads((rd / "config.json").read_text(encoding="utf-8"))
    rows = []
    for iid in cfg["tasks"]:
        e = state["tasks"].get(iid, {})
        last = (e.get("attempts") or [{}])[-1]
        rows.append({
            "instance_id": iid, "status": e.get("status", "not_run"), "attempts": len(e.get("attempts", [])),
            "submission_complete": e.get("submission_complete"), "wall_s": last.get("wall_s"),
            "api_calls": last.get("api_calls"), "total_tokens": last.get("total_tokens"),
            "figures_ok": last.get("figures_ok"), "figures_failed": last.get("figures_failed"),
            "missing_image_refs": len(last.get("missing_image_refs") or []),
        })
    per_task_scores = {}
    for csv_path in sorted((rd / "scores").glob("*.csv")) if (rd / "scores").is_dir() else []:
        if csv_path.name == "overall_results.csv":
            continue
        with open(csv_path, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                per_task_scores[r.get("instance_id")] = r
    for r in rows:
        s = per_task_scores.get(r["instance_id"])
        r["score"] = _num(s.get("weighted_total_score")) if s else None
        r["rubric_score"] = s.get("rubrics_total_score") if s else None
    write_result_jsons(rd, cfg, state, per_task_scores)
    with open(rd / "summary.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\nrun {args.run_name}: {len(rows)} tasks, status counts {counts}")
    for r in rows:
        print("  " + "  ".join(f"{k}={v}" for k, v in r.items()))
    overall = rd / "scores" / "overall_results.csv"
    if overall.is_file():
        print("\nofficial overall_results.csv:\n" + overall.read_text(encoding="utf-8"))


def cmd_sample(args) -> None:
    root = Path(args.root).expanduser()
    ids = sorted(load_tasks(root))
    picked = sorted(random.Random(args.seed).sample(ids, args.n))
    Path(args.out).write_text(f"# random.Random({args.seed}).sample(all {len(ids)} ids, {args.n})\n"
                              + "\n".join(picked) + "\n", encoding="utf-8")
    print("\n".join(picked))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="~/dacomp_bench")
    ap.add_argument("--daclaw-home", default="~/.daclaw", help="source of config.yaml/.env")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample")
    s.add_argument("--n", type=int, default=10)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--out", required=True)

    r = sub.add_parser("run")
    r.add_argument("--run-name", required=True)
    r.add_argument("--tasks", required=True, help="file with one instance id per line")
    r.add_argument("--image", default="daclaw:v0.3")
    r.add_argument("--model", default="gpt-6.1-sol")
    r.add_argument("--max-iterations", type=int, default=0, help="0 = product setting from config.yaml agent.max_turns (90)")
    r.add_argument("--temperature", type=float, default=None)
    r.add_argument("--timeout", type=int, default=7200, help="seconds per attempt (guards against hangs; max-iterations bounds the work)")
    r.add_argument("--max-retries", type=int, default=2, help="retries for infrastructure failures only")
    r.add_argument("--workers", type=int, default=3)
    r.add_argument("--memory", default="8g")
    r.add_argument("--cpus", type=float, default=4)
    r.add_argument("--env-keys", default="OPENAI_API_KEY")
    r.add_argument("--force-config", action="store_true")

    j = sub.add_parser("judge")
    j.add_argument("--run-name", required=True)
    j.add_argument("--judge-model", default="gpt-6.1-sol")
    j.add_argument("--judge-base-url", default="https://penguinapi.org/v1")
    j.add_argument("--judge-key-env", default="OPENAI_API_KEY")
    j.add_argument("--judge-max-tokens", type=int, default=16384)
    j.add_argument("--judge-timeout", type=int, default=600)
    j.add_argument("--judge-reasoning-effort", default="low",
                   help="passed to reasoning judges; the relay drops (HTTP 524) single generations that run too long")
    j.add_argument("--judge-workers", type=int, default=4)
    j.add_argument("--judge-python", default=os.path.expanduser("~/dacomp_bench/judge-venv/bin/python"))

    m = sub.add_parser("summary")
    m.add_argument("--run-name", required=True)

    args = ap.parse_args()
    {"sample": cmd_sample, "run": cmd_run, "judge": cmd_judge, "summary": cmd_summary}[args.cmd](args)


if __name__ == "__main__":
    main()
