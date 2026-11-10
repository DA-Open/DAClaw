"""
GDPval Evaluation Environment

Eval-only Atropos environment for openai/gdpval -- 220 real-world
knowledge-work tasks across 44 occupations (9 sectors). Each task ships a prompt,
one or more reference files and a scoring rubric; the agent must produce the
deliverable files the prompt asks for.

Nothing here is trained on: the environment is driven by the "evaluate"
subcommand, exactly like TerminalBench2EvalEnv (see environments/README.md).

Per task:
    1. Materialise a private workspace: the task's reference files are symlinked
       in under their original basenames (1.6 GB total, so never copied).
    2. Point the terminal tool's cwd at that workspace (per-task override).
    3. Run DAClawAgentLoop.
    4. Harvest the files the agent created and grade them against the rubric
       (deterministic rules first, LLM judge for the rest).
    5. Persist the per-task record, then clean the workspace.

Usage:
    python environments/benchmarks/gdpval/gdpval_env.py evaluate
        --config environments/benchmarks/gdpval/default.yaml

    # Smoke-test three tasks:
    python environments/benchmarks/gdpval/gdpval_env.py evaluate
        --config environments/benchmarks/gdpval/default.yaml --env.limit 3
"""

import asyncio
import datetime
import json
import logging
import os
import shutil
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_repo_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# atroposlib loads AutoTokenizer in BaseEnv.__init__ even though eval mode only
# needs placeholder token counts. On hosts with no egress the Hub check stalls
# for minutes, so stay offline and use the cached tokenizer (fetch it once with
# a proxy if missing).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from pydantic import Field

from atroposlib.envs.base import EvalHandlingEnum
from atroposlib.envs.server_handling.server_manager import APIServerConfig

from environments.agent_loop import AgentResult, DAClawAgentLoop
from environments.daclaw_base_env import DAClawAgentBaseEnv, DAClawAgentEnvConfig
from environments.benchmarks.gdpval import gdpval_grader as grader
from tools.terminal_tool import (
    clear_task_env_overrides,
    cleanup_vm,
    register_task_env_overrides,
)

logger = logging.getLogger(__name__)

DEFAULT_DATASET = "/home/yangxingchang/DA-Research/bench/gdpval"

HARNESS_INSTRUCTIONS = """

---
Harness notes (not part of the original task):
- Your current working directory already contains the files referenced above,
  under their original names. Read them from there.
- Write the final deliverable file(s) into the current working directory.
- When you are done, stop -- do not keep exploring.
"""


def _as_list(value: Any) -> List[str]:
    """Parquet list columns come back as numpy arrays -- never use truthiness on them."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        out = tolist()
        if isinstance(out, list):
            return [str(v) for v in out]
        return [str(out)]
    if isinstance(value, str):
        return [value] if value else []
    return [str(value)]


class GDPvalEvalConfig(DAClawAgentEnvConfig):
    """Configuration for the GDPval evaluation environment."""

    dataset_path: str = Field(
        default=DEFAULT_DATASET,
        description="Local GDPval release directory (holds data/*.parquet, reference_files/).",
    )
    work_dir: str = Field(
        default="environments/benchmarks/gdpval/work",
        description="Scratch directory; one sub-directory per task, removed after grading.",
    )
    results_dir: str = Field(
        default="environments/benchmarks/gdpval/evals",
        description="Where per-task JSON records and the run summary are written.",
    )

    task_timeout: int = Field(
        default=1800,
        description="Wall-clock seconds per task before it is force-failed.",
    )
    max_concurrent_tasks: int = Field(
        default=4,
        description="How many tasks run at once. Keep low: one local endpoint serves them all.",
    )

    # Task selection
    task_filter: Optional[str] = Field(
        default=None,
        description="Comma-separated task_ids to run (or unique prefixes of them).",
    )
    sector_filter: Optional[str] = Field(
        default=None,
        description="Only run tasks whose sector contains this substring (case-insensitive).",
    )
    limit: int = Field(
        default=0,
        description="Run at most N tasks after filtering (0 = all). Useful for smoke tests.",
    )

    # Grading
    judge_model: str = Field(
        default="deepseek-v4-pro",
        description="Model used as the rubric judge. Should differ from the harness model.",
    )
    judge_base_url: str = Field(
        default="http://127.0.0.1:18081/plan/v1",
        description="OpenAI-compatible endpoint for the judge.",
    )
    judge_api_key: str = Field(
        default="",
        description="Judge API key; falls back to OPENAI_API_KEY.",
    )
    judge_max_bundle_chars: int = Field(
        default=60000,
        description="How much deliverable text to show the judge per task.",
    )
    judge_batch_size: int = Field(
        default=25,
        description="Rubric items per judge call. Bigger = fewer calls, larger replies.",
    )
    grader_use_rules: bool = Field(
        default=True,
        description="Resolve filename/extension/worksheet criteria by rule instead of asking the judge.",
    )
    keep_workspaces: bool = Field(
        default=False,
        description="Keep each task's workspace after grading (debugging).",
    )


class GDPvalEvalEnv(DAClawAgentBaseEnv):
    """Evaluation-only environment for GDPval."""

    name = "gdpval"
    env_config_cls = GDPvalEvalConfig

    @classmethod
    def config_init(cls) -> Tuple[GDPvalEvalConfig, List[APIServerConfig]]:
        env_config = GDPvalEvalConfig(
            enabled_toolsets=["terminal", "file"],
            disabled_toolsets=None,
            distribution=None,

            max_agent_turns=60,
            max_token_length=32000,
            agent_temperature=0.6,
            system_prompt=None,

            terminal_backend="local",
            terminal_timeout=180,
            tool_pool_size=64,

            dataset_path=DEFAULT_DATASET,
            task_timeout=1800,
            max_concurrent_tasks=4,

            eval_handling=EvalHandlingEnum.STOP_TRAIN,
            group_size=1,
            steps_per_eval=1,
            total_steps=1,

            # atroposlib always instantiates AutoTokenizer, even in eval-only
            # mode (Phase 1 only uses it for placeholder token counts).
            tokenizer_name="gpt2",
            use_wandb=False,
            wandb_name="gdpval",
            ensure_scores_are_not_same=False,
        )

        server_configs = [
            APIServerConfig(
                base_url=os.getenv("GDPVAL_API_BASE", "http://127.0.0.1:18081/plan/v1"),
                model_name=os.getenv("GDPVAL_MODEL", "deepseek-v4-flash"),
                server_type="openai",
                api_key=os.getenv("OPENAI_API_KEY", ""),
                health_check=False,
            )
        ]
        return env_config, server_configs

    # ------------------------------------------------------------------ setup
    async def setup(self):
        """Load the local parquet release and prepare per-run bookkeeping."""
        self.dataset_root = Path(self.config.dataset_path).expanduser().resolve()
        parquet = self.dataset_root / "data" / "train-00000-of-00001.parquet"
        if not parquet.is_file():
            raise FileNotFoundError(
                "GDPval parquet not found at %s -- check env.dataset_path" % parquet
            )

        self.work_root = Path(self.config.work_dir).expanduser().resolve()
        self.results_root = Path(self.config.results_dir).expanduser().resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        (self.results_root / "tasks").mkdir(parents=True, exist_ok=True)

        # Keep sandboxes alive for the whole task, then let the reaper clean up.
        lifetime = self.config.task_timeout + 120
        self.config.terminal_lifetime = lifetime
        os.environ["TERMINAL_LIFETIME_SECONDS"] = str(lifetime)

        tasks = self._load_tasks()
        self.all_eval_items = tasks
        self.iter = 0

        self.sector_index: Dict[str, List[str]] = defaultdict(list)
        for t in tasks:
            self.sector_index[t["sector"]].append(t["task_id"])

        self.eval_metrics: List[Tuple[str, float]] = []
        self.judge = self._build_judge()

        run_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = "gdpval_%s" % run_ts
        self._streaming_path = self.results_root / ("samples_%s.jsonl" % run_ts)
        self._streaming_file = open(self._streaming_path, "w", encoding="utf-8")

        print("GDPval ready: %d tasks across %d sectors" % (len(tasks), len(self.sector_index)))
        for sector, ids in sorted(self.sector_index.items()):
            print("  %-45s %d" % (sector[:45], len(ids)))
        print("  judge: %s @ %s" % (self.config.judge_model, self.config.judge_base_url))
        print("  streaming to: %s" % self._streaming_path)

    def _load_tasks(self) -> List[Dict[str, Any]]:
        import pandas as pd

        parquet = self.dataset_root / "data" / "train-00000-of-00001.parquet"
        df = pd.read_parquet(parquet)
        tasks: List[Dict[str, Any]] = []
        for row in df.itertuples(index=False):
            record = row._asdict()
            record["reference_files"] = _as_list(record.get("reference_files"))
            record["deliverable_files"] = _as_list(record.get("deliverable_files"))
            tasks.append(record)

        if self.config.sector_filter:
            needle = self.config.sector_filter.lower()
            before = len(tasks)
            tasks = [t for t in tasks if needle in str(t.get("sector", "")).lower()]
            print("  sector filter %r: %d -> %d tasks" % (self.config.sector_filter, before, len(tasks)))

        if self.config.task_filter:
            wanted = [x.strip() for x in self.config.task_filter.split(",") if x.strip()]
            tasks = [
                t for t in tasks
                if any(str(t["task_id"]).startswith(w) for w in wanted)
            ]
            print("  task filter kept %d tasks" % len(tasks))

        if self.config.limit and self.config.limit > 0:
            tasks = tasks[: self.config.limit]
            print("  limited to %d tasks" % len(tasks))

        if not tasks:
            raise RuntimeError("No GDPval tasks selected -- check task_filter/sector_filter/limit")
        return tasks

    def _build_judge(self) -> Optional[grader.JudgeClient]:
        api_key = self.config.judge_api_key or os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            print("  WARNING: no judge API key -- every judge-only criterion scores 0")
            return None
        return grader.JudgeClient(
            base_url=self.config.judge_base_url,
            api_key=api_key,
            model=self.config.judge_model,
        )

    # ------------------------------------------------- training stubs (unused)
    async def get_next_item(self):
        item = self.all_eval_items[self.iter % len(self.all_eval_items)]
        self.iter += 1
        return item

    def format_prompt(self, item) -> str:
        return str(item.get("prompt", "")) + HARNESS_INSTRUCTIONS

    async def compute_reward(self, item, result: AgentResult, ctx) -> float:
        """Unused in eval mode; grading happens in rollout_and_score_eval."""
        return 0.0

    # -------------------------------------------------------------- workspace
    def _materialize(self, item: Dict[str, Any]) -> Tuple[Path, List[str]]:
        """Create the task workspace and symlink its reference files in."""
        ws = self.work_root / str(item["task_id"])
        if ws.exists():
            shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir(parents=True, exist_ok=True)

        seeded: List[str] = []
        for idx, rel in enumerate(item.get("reference_files") or []):
            src = (self.dataset_root / rel).expanduser()
            if not src.is_file():
                logger.warning("missing reference file: %s", src)
                continue
            name = Path(rel).name
            dst = ws / name
            if dst.exists():
                name = "%02d_%s" % (idx, Path(rel).name)
                dst = ws / name
            try:
                dst.symlink_to(src.resolve())
            except OSError:
                shutil.copy2(src, dst)
            seeded.append(name)
        return ws, seeded

    # ------------------------------------------------------------------ eval
    async def rollout_and_score_eval(self, eval_item: Dict[str, Any]) -> Dict[str, Any]:
        """Run one GDPval task end to end and grade its deliverable."""
        from tqdm import tqdm

        task_id = str(eval_item["task_id"])
        sector = str(eval_item.get("sector", "unknown"))
        rollout_id = str(uuid.uuid4())
        started = time.time()
        tqdm.write("  [START] %s (%s)" % (task_id[:8], sector[:40]))

        ws, seeded = self._materialize(eval_item)
        record: Dict[str, Any] = {
            "task_id": task_id,
            "sector": sector,
            "occupation": eval_item.get("occupation"),
            "workspace": str(ws),
        }

        try:
            register_task_env_overrides(rollout_id, {"cwd": str(ws)})

            tools, valid_names = self._resolve_tools_for_group()
            messages: List[Dict[str, Any]] = []
            if self.config.system_prompt:
                messages.append({"role": "system", "content": self.config.system_prompt})
            messages.append({"role": "user", "content": self.format_prompt(eval_item)})

            result = await self._run_agent(messages, tools, valid_names, rollout_id)
            record["turns_used"] = result.turns_used
            record["finished_naturally"] = result.finished_naturally
            record["messages"] = result.messages
            record["tool_errors"] = [
                {"turn": e.turn, "tool": e.tool_name, "error": e.error[:300]}
                for e in (result.tool_errors or [])
            ]

            files = grader.collect_deliverables(ws, seeded)
            items = grader.load_rubric(eval_item.get("rubric_json"))
            grade = await grader.grade_task(
                prompt=str(eval_item.get("prompt", "")),
                files=files,
                items=items,
                judge=self.judge,
                max_bundle_chars=self.config.judge_max_bundle_chars,
                use_rules=self.config.grader_use_rules,
                batch_size=self.config.judge_batch_size,
            )
            record["grade"] = grade
            record["reward"] = grade["score"] / 100.0
            record["score"] = grade["score"]
            record["passed"] = grade["score"] > 0
            record["error"] = None if files else "no_deliverable"

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("task %s failed", task_id)
            record.update(
                {"score": 0.0, "reward": 0.0, "passed": False, "error": str(exc)[:500]}
            )
        finally:
            record["elapsed_seconds"] = round(time.time() - started, 1)
            clear_task_env_overrides(rollout_id)
            try:
                cleanup_vm(rollout_id)
            except Exception:  # noqa: BLE001
                pass
            if not self.config.keep_workspaces:
                shutil.rmtree(ws, ignore_errors=True)
                record["workspace"] = None

        self._save_result(record)
        tqdm.write(
            "  [DONE] %s score=%.1f turns=%s %ss"
            % (task_id[:8], record.get("score", 0.0), record.get("turns_used", "?"),
               record.get("elapsed_seconds"))
        )
        return record

    async def _run_agent(self, messages, tools, valid_names, rollout_id: str) -> AgentResult:
        if self._use_managed_server():
            async with self.server.managed_server(
                tokenizer=self.tokenizer,
                preserve_think_blocks=bool(self.config.thinking_mode),
            ) as managed:
                agent = DAClawAgentLoop(
                    server=managed,
                    tool_schemas=tools,
                    valid_tool_names=valid_names,
                    max_turns=self.config.max_agent_turns,
                    task_id=rollout_id,
                    temperature=self.config.agent_temperature,
                    max_tokens=self.config.max_token_length,
                    extra_body=self.config.extra_body,
                    budget_config=self.config.build_budget_config(),
                )
                return await agent.run(messages)

        agent = DAClawAgentLoop(
            server=self.server,
            tool_schemas=tools,
            valid_tool_names=valid_names,
            max_turns=self.config.max_agent_turns,
            task_id=rollout_id,
            temperature=self.config.agent_temperature,
            max_tokens=self.config.max_token_length,
            extra_body=self.config.extra_body,
            budget_config=self.config.build_budget_config(),
        )
        return await agent.run(messages)

    def _save_result(self, record: Dict[str, Any]) -> None:
        """One line per finished task: crash-safe progress trail."""
        try:
            with open(self._streaming_file.name, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            task_file = self.results_root / "tasks" / ("%s.json" % record["task_id"])
            task_file.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to persist result for %s: %s", record.get("task_id"), exc)

    async def _eval_with_timeout(self, item: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self.rollout_and_score_eval(item), timeout=self.config.task_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("task %s timed out after %ss", item["task_id"], self.config.task_timeout)
            record = {
                "task_id": str(item["task_id"]),
                "sector": item.get("sector", "unknown"),
                "score": 0.0,
                "reward": 0.0,
                "passed": False,
                "error": "timeout",
            }
            self._save_result(record)
            return record

    async def evaluate(self, *args, **kwargs) -> None:
        """Entry point for the evaluate subcommand."""
        from tqdm import tqdm

        class _TqdmHandler(logging.Handler):
            def emit(self, record):
                try:
                    tqdm.write(self.format(record))
                except Exception:  # noqa: BLE001
                    self.handleError(record)

        handler = _TqdmHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
        )
        root = logging.getLogger()
        root.handlers = [handler]
        root.setLevel(logging.INFO)
        for noisy in ("httpx", "openai", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        start_time = time.time()
        total_tasks = len(self.all_eval_items)
        print("\n%s" % ("=" * 62))
        print("Starting GDPval evaluation")
        print("%s" % ("=" * 62))
        print("  Tasks:              %d" % total_tasks)
        print("  Max agent turns:    %d" % self.config.max_agent_turns)
        print("  Task timeout:       %ss" % self.config.task_timeout)
        print("  Terminal backend:   %s" % self.config.terminal_backend)
        print("  Concurrency:        %d" % self.config.max_concurrent_tasks)
        print("%s\n" % ("=" * 62))

        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrent_tasks))

        async def _guarded(item):
            async with semaphore:
                return await self._eval_with_timeout(item)

        eval_tasks = [asyncio.ensure_future(_guarded(i)) for i in self.all_eval_items]
        results: List[Dict[str, Any]] = []
        scores: List[float] = []
        pbar = tqdm(total=total_tasks, desc="Evaluating GDPval", dynamic_ncols=True)

        try:
            for coro in asyncio.as_completed(eval_tasks):
                res = await coro
                results.append(res)
                scores.append(float(res.get("score") or 0.0))
                mean = sum(scores) / len(scores)
                pbar.set_postfix_str("mean=%.1f n=%d" % (mean, len(scores)))
                pbar.update(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pbar.close()
            print("\nInterrupted -- cancelling %d in-flight tasks..." % len(eval_tasks))
            for t in eval_tasks:
                t.cancel()
            await asyncio.gather(*eval_tasks, return_exceptions=True)
            from tools.terminal_tool import cleanup_all_environments

            cleanup_all_environments()
            return
        finally:
            pbar.close()

        end_time = time.time()
        valid = [r for r in results if r]
        if not valid:
            print("No results produced.")
            return

        overall = sum(float(r.get("score") or 0.0) for r in valid) / len(valid)
        solved = sum(1 for r in valid if float(r.get("score") or 0.0) > 0)
        errored = sum(1 for r in valid if r.get("error"))

        eval_metrics: Dict[str, float] = {
            "eval/score_mean": overall,
            "eval/tasks": len(valid),
            "eval/tasks_with_deliverable": solved,
            "eval/tasks_errored": errored,
            "eval/evaluation_time_seconds": end_time - start_time,
        }

        by_sector: Dict[str, List[float]] = defaultdict(list)
        for r in valid:
            by_sector[str(r.get("sector", "unknown"))].append(float(r.get("score") or 0.0))
        for sector, vals in sorted(by_sector.items()):
            key = sector.replace(" ", "_").replace(",", "").replace("-", "_").lower()[:40]
            eval_metrics["eval/sector_%s" % key] = sum(vals) / len(vals)

        self.eval_metrics = list(eval_metrics.items())

        print("\n%s" % ("=" * 62))
        print("GDPval Results")
        print("%s" % ("=" * 62))
        print("Mean rubric score: %.2f / 100  (%d tasks)" % (overall, len(valid)))
        print("Tasks with a deliverable: %d/%d" % (solved, len(valid)))
        print("Tasks errored: %d" % errored)
        print("Wall clock: %.1fs" % (end_time - start_time))

        print("\nBy sector:")
        for sector, vals in sorted(by_sector.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
            print("  %-45s %6.2f  (n=%d)" % (sector[:45], sum(vals) / len(vals), len(vals)))

        print("\nPer task:")
        for r in sorted(valid, key=lambda x: float(x.get("score") or 0.0)):
            print(
                "  %6.2f  %s  %-38s turns=%s%s"
                % (
                    float(r.get("score") or 0.0),
                    str(r.get("task_id"))[:8],
                    str(r.get("sector"))[:38],
                    r.get("turns_used", "?"),
                    ("  (%s)" % r["error"]) if r.get("error") else "",
                )
            )
        print("%s\n" % ("=" * 62))

        summary = {
            "run_id": self.run_id,
            "score_mean": overall,
            "tasks": len(valid),
            "metrics": eval_metrics,
            "scores": {r["task_id"]: r.get("score") for r in valid},
        }
        (self.results_root / ("summary_%s.json" % self.run_id)).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        samples = [
            {
                "task_id": r.get("task_id"),
                "sector": r.get("sector"),
                "score": r.get("score"),
                "turns_used": r.get("turns_used"),
                "error": r.get("error"),
                "grade_items": (r.get("grade") or {}).get("items"),
                "messages": r.get("messages"),
            }
            for r in valid
        ]

        try:
            await self.evaluate_log(
                metrics=eval_metrics,
                samples=samples,
                start_time=start_time,
                end_time=end_time,
                generation_parameters={
                    "temperature": self.config.agent_temperature,
                    "max_tokens": self.config.max_token_length,
                    "max_agent_turns": self.config.max_agent_turns,
                    "terminal_backend": self.config.terminal_backend,
                    "judge_model": self.config.judge_model,
                },
            )
        except Exception as exc:  # noqa: BLE001
            print("evaluate_log failed: %s" % exc)

        if not self._streaming_file.closed:
            self._streaming_file.close()

        from tools.terminal_tool import cleanup_all_environments

        print("Cleaning up environments...")
        cleanup_all_environments()
        print("Done. Summary written to %s" % (self.results_root / ("summary_%s.json" % self.run_id)))

    async def wandb_log(self, wandb_metrics: Optional[Dict[str, Any]] = None):
        wandb_metrics = wandb_metrics or {}
        for name, value in self.eval_metrics:
            wandb_metrics[name] = value
        self.eval_metrics = []
        await super().wandb_log(wandb_metrics)


if __name__ == "__main__":
    GDPvalEvalEnv.cli()
