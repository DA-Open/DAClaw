"""[cyan][bold]Run SWE-agent on a single instance taken from github or similar.[/bold][/cyan]

[cyan][bold]=== BASIC OPTIONS ===[/bold][/cyan]

  -h --help           Show help text and exit
  --help_option      Print specific help text and exit
  --config CONFIG     Load additional config files. Use this option multiple times to load
                      multiple files, e.g., --config config1.yaml --config config2.yaml

[cyan][bold]=== EXAMPLES ===[/bold][/cyan]

Basic usage: Run over a [bold][cyan]github issue[/bold][/cyan][green]:

sweagent run --config config/default.yaml --agent.model.name "gpt-4o" \\
    --env.repo.github_url=https://github.com/SWE-agent/test-repo/ \\
    --problem_statement.github_url=https://github.com/SWE-agent/test-repo/issues/1
[/green]

By default this will start a docker container and run the agent in there.
You can set the image with [green]--env.docker.image[/green].

Here's an example that uses [bold][cyan]modal[/bold][/cyan] instead of docker and also a [bold][cyan]local repository[/bold][/cyan]:

[green]sweagent run --config config/default.yaml --agent.model.name "gpt-4o" \\
    --env.deployment.type=modal --env.repo.path /path/to/repo \\
    --problem_statement.path=path/to/problem_statement.md
[/green]
"""

import getpass
import json
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from sweagent.agent.agents import AbstractAgent, AgentConfig, get_agent_from_config
from sweagent.agent.problem_statement import (
    EmptyProblemStatement,
    ProblemStatement,
    ProblemStatementConfig,
    SpreadsheetProblemStatement,
)
from sweagent.environment.swe_env import EnvironmentConfig, SWEEnv
from sweagent.run.common import AutoCorrectSuggestion as ACS
from sweagent.run.common import BasicCLI, ConfigHelper, save_predictions
from sweagent.run.hooks.abstract import CombinedRunHooks, RunHook
from sweagent.run.hooks.apply_patch import SaveApplyPatchHook
from sweagent.run.hooks.open_pr import OpenPRConfig, OpenPRHook
from sweagent.utils.config import load_environment_variables
from sweagent.utils.log import (
    add_file_handler,
    get_logger,
    get_thread_log_suffix,
    register_thread_name,
    remove_file_handler,
)

logger = get_logger("swea-run", emoji="🏃")


class RunSingleActionConfig(BaseModel):
    """Run real-life actions (opening PRs, etc.) if we can solve the issue."""

    # Open a PR with the patch if we can solve the issue
    open_pr: bool = False
    pr_config: OpenPRConfig = Field(default_factory=OpenPRConfig)
    # When working with local repository: Apply patch
    apply_patch_locally: bool = False

    # pydantic config
    model_config = ConfigDict(extra="forbid")


def _get_default_output_dir(output_dir: Path, problem_statement: ProblemStatement, agent: AgentConfig) -> Path:
    if output_dir == Path("DEFAULT"):
        user_id = getpass.getuser()
        problem_id = problem_statement.id
        try:
            model_id = agent.model.id  # type: ignore[attr-defined]
        except AttributeError:
            model_id = "unknown_model"
        config_file = getattr(agent, "_config_files", ["no_config"])[0]
        if isinstance(config_file, Path):
            config_file = config_file.stem
        return Path.cwd() / "trajectories" / user_id / f"{config_file}__{model_id}___{problem_id}"
    return output_dir


class RunSingleConfig(BaseSettings, cli_implicit_flags=False):
    env: EnvironmentConfig = Field(default_factory=EnvironmentConfig, description="Environment options.")
    agent: AgentConfig = Field(description="Agent options.")
    problem_statement: ProblemStatementConfig = Field(
        default_factory=EmptyProblemStatement, description="Problem statement options."
    )
    output_dir: Path = Field(default=Path("DEFAULT"), description="Output directory.")

    actions: RunSingleActionConfig = Field(default_factory=RunSingleActionConfig)

    env_var_path: Path | None = None
    """Path to a .env file to load environment variables from."""

    num_workers: int = Field(default=1, ge=1, description="Number of parallel SpreadsheetBench tasks to run.")
    """Only used together with ``--dataset_path``. Each worker starts an independent environment."""

    # pydantic config
    model_config = SettingsConfigDict(extra="forbid", env_prefix="SWE_AGENT_")

    def set_default_output_dir(self) -> None:
        # Needs to be called explicitly, because self._config_files will be setup
        # post-init.
        self.output_dir = _get_default_output_dir(self.output_dir, self.problem_statement, self.agent)

    @classmethod
    def _get_auto_correct(cls) -> list[ACS]:
        return [
            ACS("model", "agent.model.name"),
            ACS("agent.model", "agent.model.name"),
            ACS("model.name", "agent.model.name"),
            ACS("per_instance_cost_limit", "agent.model.per_instance_cost_limit"),
            ACS("model.per_instance_cost_limit", "agent.model.per_instance_cost_limit"),
            ACS("config_file", "config"),
            ACS(
                "data_path",
                help="--data_path is no longer support for SWE-A 1.0. Please check the tutorial and use one of the --problem_statement options, e.g., --problem_statement.github_url or --problem_statement.path",
            ),
            ACS(
                "repo_path",
                help="--repo_path is no longer support for SWE-A 1.0. Please check the tutorial and use one of the --env.repo options, e.g., --env.repo.github_url or --env.repo.path",
            ),
            ACS("repo.path", "env.repo.path"),
        ]


class RunSingle:
    def __init__(
        self,
        env: SWEEnv,
        agent: AbstractAgent,
        problem_statement: ProblemStatement | ProblemStatementConfig,
        *,
        output_dir: Path = Path("."),
        hooks: list[RunHook] | None = None,
        actions: RunSingleActionConfig | None = None,
    ):
        """Note: When initializing this class, make sure to add the hooks that are required by your actions.
        See `from_config` for an example.
        """
        self.logger = get_logger("swea-run", emoji="🏃")
        self._log_handler_ids: list[str] = []
        instance_id = problem_statement.id
        current_task_id = get_thread_log_suffix()
        record_filter = None
        if current_task_id == instance_id:
            record_filter = lambda record: get_thread_log_suffix(record.threadName) == instance_id
        _log_filename_template = f"{instance_id}.{{level}}.log"
        for level in ["trace", "debug", "info"]:
            handler_id = add_file_handler(
                output_dir / instance_id / _log_filename_template.format(level=level),
                record_filter=record_filter,
                level=level,
                id_=f"{instance_id}-{level}",
            )
            self._log_handler_ids.append(handler_id)
        self.env = env
        self.agent = agent
        self.output_dir = output_dir
        self._hooks = []
        if actions is not None:
            actions = RunSingleActionConfig()
        self.actions = actions
        self._chooks = CombinedRunHooks()
        self.problem_statement = problem_statement
        for hook in hooks or []:
            self.add_hook(hook)

    @property
    def hooks(self) -> list[RunHook]:
        return self._chooks.hooks

    @classmethod
    def from_config(cls, config: RunSingleConfig) -> Self:
        load_environment_variables(config.env_var_path)
        config.set_default_output_dir()
        config.output_dir.mkdir(parents=True, exist_ok=True)
        agent = get_agent_from_config(config.agent)
        agent.replay_config = config  # type: ignore[attr-defined]
        self = cls(
            env=SWEEnv.from_config(config.env),
            agent=agent,
            problem_statement=config.problem_statement,
            output_dir=config.output_dir,
            actions=config.actions,
        )
        self.add_hook(SaveApplyPatchHook(apply_patch_locally=config.actions.apply_patch_locally))
        if config.actions.open_pr:
            self.logger.debug("Adding OpenPRHook")
            self.add_hook(OpenPRHook(config.actions.pr_config))
        return self

    def add_hook(self, hook: RunHook) -> None:
        hook.on_init(run=self)
        self._chooks.add_hook(hook)

    def run(self):
        self._chooks.on_start()
        try:
            self.logger.info("Starting environment")
            self.env.start()
            self.logger.info("Running agent")
            self._chooks.on_instance_start(index=0, env=self.env, problem_statement=self.problem_statement)
            output_dir = self.output_dir / self.problem_statement.id
            output_dir.mkdir(parents=True, exist_ok=True)
            if self.agent.replay_config is not None:  # type: ignore[attr-defined]
                (output_dir / "config.yaml").write_text(
                    yaml.dump(self.agent.replay_config.model_dump_json(), indent=2)  # type: ignore[attr-defined]
                )
            result = self.agent.run(
                problem_statement=self.problem_statement,
                env=self.env,
                output_dir=output_dir,
            )
            self._chooks.on_instance_completed(result=result)
            self.logger.info("Done")
            self._chooks.on_end()
            save_predictions(self.output_dir, self.problem_statement.id, result)
        finally:
            self.env.close()
            for handler_id in self._log_handler_ids:
                remove_file_handler(handler_id)


def run_from_config(config: RunSingleConfig):
    RunSingle.from_config(config).run()


def _prepare_task_dataset_mount(input_path: Path, relative_input_path: Path, target_root: Path) -> Path:
    """Create a Docker mount containing only the current task's input file."""
    if relative_input_path.is_absolute() or ".." in relative_input_path.parts:
        raise ValueError(f"Spreadsheet path must stay inside the dataset: {relative_input_path}")

    staged_input_path = target_root / relative_input_path
    staged_input_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(input_path, staged_input_path)
    return target_root


def _mount_targets_path(spec: str, container_path: str, *, mount_syntax: bool = False) -> bool:
    """Return whether a Docker volume/mount spec targets container_path or one of its children."""
    if mount_syntax:
        target = next(
            (
                value
                for field in spec.split(",")
                for key, separator, value in [field.partition("=")]
                if separator and key.strip().lower() in {"target", "dst", "destination"}
            ),
            "",
        )
    else:
        fields = spec.rsplit(":", 2)
        if len(fields) == 1:
            target = fields[0]
        else:
            target = fields[-1] if fields[-1].startswith("/") else fields[-2]

    target = target.strip().rstrip("/")
    container_path = container_path.rstrip("/")
    return target == container_path or target.startswith(f"{container_path}/")


def _replace_path_mount(
    docker_args: list[str],
    host_path: Path,
    container_path: str,
    *,
    read_only: bool,
) -> list[str]:
    """Replace all mounts targeting ``container_path`` with a task-specific mount."""
    cleaned_args: list[str] = []
    i = 0
    while i < len(docker_args):
        arg = docker_args[i]
        if arg in {"-v", "--volume", "--mount"} and i + 1 < len(docker_args):
            spec = str(docker_args[i + 1])
            if _mount_targets_path(spec, container_path, mount_syntax=arg == "--mount"):
                i += 2
                continue
        elif arg.startswith(("-v=", "--volume=")):
            if _mount_targets_path(arg.split("=", 1)[1], container_path):
                i += 1
                continue
        elif arg.startswith("--mount="):
            if _mount_targets_path(arg.split("=", 1)[1], container_path, mount_syntax=True):
                i += 1
                continue

        cleaned_args.append(arg)
        i += 1

    mode = ":ro" if read_only else ""
    return [*cleaned_args, "-v", f"{host_path.resolve()}:{container_path}{mode}"]


def _replace_dataset_mount(docker_args: list[str], host_path: Path, container_path: str) -> list[str]:
    """Replace all existing dataset mounts with one read-only task-specific mount."""
    return _replace_path_mount(docker_args, host_path, container_path, read_only=True)


def _replace_output_mount(docker_args: list[str], host_path: Path, container_path: str) -> list[str]:
    """Replace all existing output mounts with one writable task-specific mount."""
    return _replace_path_mount(docker_args, host_path, container_path, read_only=False)


def _run_spreadsheet_task(
    *,
    task_data: dict[str, Any],
    task_index: int,
    config: RunSingleConfig,
    dataset_path: Path,
    output_dir: Path,
    output_base: Path,
    container_data_path: str,
    container_output_root: str,
) -> Path:
    """Run one SpreadsheetBench task in an isolated Docker environment."""
    from swerex.deployment.config import DockerDeploymentConfig

    task_id = str(task_data.get("id", f"task_{task_index}"))
    relative_input_path = Path(task_data["spreadsheet_path"])
    host_input_path = dataset_path / relative_input_path
    if not host_input_path.is_file():
        raise FileNotFoundError(f"Spreadsheet file missing for task {task_id}: {host_input_path}")

    host_output_path = output_base / f"{task_id}_output.xlsx"
    host_output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"sweagent-task-{task_index}-") as task_tempdir:
        task_temp_root = Path(task_tempdir)
        task_input_root = task_temp_root / "input"
        task_output_root = task_temp_root / "output"
        task_output_root.mkdir(parents=True, exist_ok=True)
        docker_dataset_path = _prepare_task_dataset_mount(
            host_input_path,
            relative_input_path,
            task_input_root,
        )

        task_env = config.env.model_copy(deep=True)
        if not isinstance(task_env.deployment, DockerDeploymentConfig):
            raise ValueError("SpreadsheetBench dataset execution requires a Docker deployment")

        docker_args = list(task_env.deployment.docker_args or [])
        docker_args = _replace_dataset_mount(docker_args, docker_dataset_path, container_data_path)
        docker_args = _replace_output_mount(docker_args, task_output_root, container_output_root)
        task_env.deployment.docker_args = docker_args
        logger.info(
            "Task %s mounts only input %s (read-only) and its private output directory",
            task_id,
            host_input_path,
        )

        container_input_path = f"{container_data_path}/{relative_input_path.as_posix()}"
        output_filename = f"{task_id}_output.xlsx"
        container_output_path = f"{container_output_root}/{output_filename}"
        problem_statement = SpreadsheetProblemStatement(
            instruction=task_data["instruction"],
            spreadsheet_path=container_input_path,
            output_path=container_output_path,
            id=task_id,
        )

        # Deep-copy the complete config so agents, actions, environments and Docker args
        # are never shared between worker threads.
        task_config = config.model_copy(
            deep=True,
            update={
                "env": task_env,
                "problem_statement": problem_statement,
                "output_dir": output_dir,
                "num_workers": 1,
            },
        )
        run_from_config(task_config)

        staged_output_path = task_output_root / output_filename
        if not staged_output_path.is_file():
            raise FileNotFoundError(f"Task {task_id} did not create the expected output: {container_output_path}")

        # The container has stopped at this point, so publishing cannot race with a writer.
        shutil.copy2(staged_output_path, host_output_path)

    return host_output_path


def _execute_dataset_tasks(
    dataset: list[dict[str, Any]],
    num_workers: int,
    run_task: Callable[[int, dict[str, Any]], Path],
) -> tuple[int, int]:
    """Execute tasks with failure isolation and return (completed, failed)."""
    if not dataset:
        return 0, 0

    max_workers = min(num_workers, len(dataset))
    completed = 0
    failed = 0

    if max_workers == 1:
        for task_index, task_data in enumerate(dataset):
            task_id = str(task_data.get("id", f"task_{task_index}"))
            logger.info("Processing task %d/%d: %s", task_index + 1, len(dataset), task_id)
            try:
                output_path = run_task(task_index, task_data)
            except Exception:
                failed += 1
                logger.exception("Task %s failed", task_id)
            else:
                completed += 1
                logger.info("Completed task %s: %s", task_id, output_path)
        return completed, failed

    logger.info("Running up to %d tasks concurrently; each task uses its own Docker environment", max_workers)

    def run_task_in_worker(task_index: int, task_data: dict[str, Any]) -> Path:
        task_id = str(task_data.get("id", f"task_{task_index}"))
        register_thread_name(task_id)
        return run_task(task_index, task_data)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="spreadsheet-task") as executor:
        futures = {
            executor.submit(run_task_in_worker, task_index, task_data): (task_index, task_data)
            for task_index, task_data in enumerate(dataset)
        }
        for future in as_completed(futures):
            task_index, task_data = futures[future]
            task_id = str(task_data.get("id", f"task_{task_index}"))
            try:
                output_path = future.result()
            except Exception:
                failed += 1
                logger.exception("Task %s failed", task_id)
            else:
                completed += 1
                logger.info("Completed task %s: %s", task_id, output_path)

    return completed, failed


def run_from_cli(args: list[str] | None = None):
    if args is None:
        args = sys.argv[1:]
    
    # Check if dataset_path is provided
    dataset_path = None
    remaining_args = []
    i = 0
    while i < len(args):
        if args[i] == '--dataset_path' and i + 1 < len(args):
            dataset_path = Path(args[i + 1])
            i += 2
        else:
            remaining_args.append(args[i])
            i += 1
    
    assert __doc__ is not None
    help_text = (  # type: ignore
        __doc__ + "\n[cyan][bold]=== ALL THE OPTIONS ===[/bold][/cyan]\n\n" + ConfigHelper().get_help(RunSingleConfig)
    )
    config = BasicCLI(RunSingleConfig, help_text=help_text).get_config(remaining_args)  # type: ignore
    
    # If dataset_path is provided, process all tasks from dataset.json.
    if dataset_path:
        container_data_path = "/mnt/spreadsheet_data"
        container_output_root = "/mnt/spreadsheet_output"

        dataset_file = dataset_path / "dataset.json"
        if not dataset_file.exists():
            raise FileNotFoundError(f"Dataset file not found: {dataset_file}")

        dataset = json.loads(dataset_file.read_text(encoding="utf-8"))
        if not isinstance(dataset, list):
            raise ValueError(f"Dataset must be a JSON list: {dataset_file}")

        task_ids = [str(task.get("id", f"task_{index}")) for index, task in enumerate(dataset)]
        duplicate_ids = sorted(task_id for task_id, count in Counter(task_ids).items() if count > 1)
        if duplicate_ids:
            raise ValueError(f"Dataset task ids must be unique; duplicates: {', '.join(duplicate_ids)}")

        logger.info("Loaded %d tasks from %s", len(dataset), dataset_file)

        # Setup output directory with config info
        model_id = str(getattr(config.agent.model, "name", "unknown_model")).replace("/", "_").replace(":", "-")
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        config_id = f"{len(dataset)}tasks_{model_id}_{run_id}"

        dataset_folder_name = dataset_path.name

        if config.output_dir == Path("DEFAULT"):
            output_dir = Path.cwd() / "trajectories" / "spreadsheet" / dataset_folder_name / config_id
        else:
            output_dir = config.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        output_base = Path.cwd() / "trajectories" / "output_excel" / dataset_folder_name / config_id
        output_base.mkdir(parents=True, exist_ok=True)

        def run_task(task_index: int, task_data: dict[str, Any]) -> Path:
            return _run_spreadsheet_task(
                task_data=task_data,
                task_index=task_index,
                config=config,
                dataset_path=dataset_path,
                output_dir=output_dir,
                output_base=output_base,
                container_data_path=container_data_path,
                container_output_root=container_output_root,
            )

        completed, failed = _execute_dataset_tasks(dataset, config.num_workers, run_task)
        logger.info("Dataset run finished: %d completed, %d failed", completed, failed)
    else:
        # Normal single task processing
        if config.num_workers != 1:
            raise ValueError("--num_workers is only supported together with --dataset_path")
        # Check if user tries to use SpreadsheetProblemStatement without dataset_path
        if isinstance(config.problem_statement, SpreadsheetProblemStatement):
            raise ValueError(
                "SpreadsheetProblemStatement is only supported with --dataset_path. "
                "Please use --dataset_path to process Excel tasks from dataset.json"
            )
        run_from_config(config)


if __name__ == "__main__":
    run_from_cli()
