import asyncio
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from swerex.deployment.config import DockerDeploymentConfig
from swerex.deployment.docker import DockerDeployment

from sweagent import CONFIG_DIR
from sweagent.environment import swe_env as swe_env_module
from sweagent.environment.swe_env import EnvironmentConfig, SWEEnv
from sweagent.run import run_single
from sweagent.run.common import BasicCLI
from sweagent.run.run_single import RunSingleActionConfig, RunSingleConfig
from sweagent.utils.log import get_logger, get_thread_log_suffix, register_thread_name


def _host_path_for_mount(docker_args: list[str], container_path: str, *, read_only: bool) -> Path:
    suffix = f":{container_path}:ro" if read_only else f":{container_path}"
    for index, argument in enumerate(docker_args):
        if argument == "-v" and index + 1 < len(docker_args):
            spec = docker_args[index + 1]
            if spec.endswith(suffix):
                return Path(spec.removesuffix(suffix))
    raise AssertionError(f"No mount found for {container_path}")


def _make_config() -> RunSingleConfig:
    environment = EnvironmentConfig(
        deployment=DockerDeploymentConfig(
            image="python:3.11",
            python_standalone_dir="/root",
            docker_args=[
                "-v",
                "/whole-dataset:/mnt/spreadsheet_data:ro",
                "-v",
                "/shared-output:/mnt/spreadsheet_output",
                "--name",
                "unrelated-option",
            ],
        )
    )
    # The tests replace run_from_config before an agent is constructed, so model_construct
    # keeps the fixture focused on task isolation without requiring an API model config.
    return RunSingleConfig.model_construct(
        env=environment,
        agent=object(),
        problem_statement=object(),
        output_dir=Path("DEFAULT"),
        actions=RunSingleActionConfig(),
        env_var_path=None,
        num_workers=2,
    )


class SpreadsheetConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_task_mounts_only_its_input_and_uses_private_output(self) -> None:
        dataset_path = self.temp_path / "dataset"
        input_path = dataset_path / "spreadsheet" / "input.xlsx"
        golden_path = dataset_path / "golden" / "golden.xlsx"
        input_path.parent.mkdir(parents=True)
        golden_path.parent.mkdir(parents=True)
        input_path.write_bytes(b"current-input")
        golden_path.write_bytes(b"must-not-be-mounted")

        config = _make_config()
        original_docker_args = list(config.env.deployment.docker_args)
        mounted_roots: list[Path] = []

        def fake_run_from_config(task_config: RunSingleConfig) -> None:
            self.assertIsNot(task_config, config)
            self.assertIsNot(task_config.env, config.env)
            docker_args = task_config.env.deployment.docker_args
            input_root = _host_path_for_mount(docker_args, "/mnt/spreadsheet_data", read_only=True)
            output_root = _host_path_for_mount(docker_args, "/mnt/spreadsheet_output", read_only=False)
            mounted_roots.extend([input_root, output_root])

            mounted_files = [
                path.relative_to(input_root).as_posix() for path in input_root.rglob("*") if path.is_file()
            ]
            self.assertEqual(mounted_files, ["spreadsheet/input.xlsx"])
            self.assertEqual((input_root / "spreadsheet" / "input.xlsx").read_bytes(), b"current-input")
            self.assertFalse((input_root / "golden" / "golden.xlsx").exists())
            self.assertEqual(
                task_config.problem_statement.spreadsheet_path,
                "/mnt/spreadsheet_data/spreadsheet/input.xlsx",
            )
            self.assertEqual(
                task_config.problem_statement.output_path,
                "/mnt/spreadsheet_output/task-a_output.xlsx",
            )

            (output_root / "task-a_output.xlsx").write_bytes(b"task-output")

        output_base = self.temp_path / "published"
        with patch.object(run_single, "run_from_config", fake_run_from_config):
            result = run_single._run_spreadsheet_task(
                task_data={
                    "id": "task-a",
                    "instruction": "edit the workbook",
                    "spreadsheet_path": "spreadsheet/input.xlsx",
                },
                task_index=0,
                config=config,
                dataset_path=dataset_path,
                output_dir=self.temp_path / "trajectories",
                output_base=output_base,
                container_data_path="/mnt/spreadsheet_data",
                container_output_root="/mnt/spreadsheet_output",
            )

        self.assertEqual(result, output_base / "task-a_output.xlsx")
        self.assertEqual(result.read_bytes(), b"task-output")
        self.assertEqual(config.env.deployment.docker_args, original_docker_args)
        self.assertTrue(all(not root.exists() for root in mounted_roots))

    def test_parallel_tasks_use_distinct_mounts_and_correct_inputs(self) -> None:
        dataset_path = self.temp_path / "dataset"
        spreadsheet_dir = dataset_path / "spreadsheet"
        spreadsheet_dir.mkdir(parents=True)
        (spreadsheet_dir / "a.xlsx").write_bytes(b"input-a")
        (spreadsheet_dir / "b.xlsx").write_bytes(b"input-b")
        (dataset_path / "golden.xlsx").write_bytes(b"golden")

        config = _make_config()
        barrier = threading.Barrier(2)
        seen_mounts: dict[str, tuple[Path, Path]] = {}
        seen_lock = threading.Lock()

        def fake_run_from_config(task_config: RunSingleConfig) -> None:
            task_id = task_config.problem_statement.id
            self.assertEqual(get_thread_log_suffix(), task_id)
            docker_args = task_config.env.deployment.docker_args
            input_root = _host_path_for_mount(docker_args, "/mnt/spreadsheet_data", read_only=True)
            output_root = _host_path_for_mount(docker_args, "/mnt/spreadsheet_output", read_only=False)
            with seen_lock:
                seen_mounts[task_id] = (input_root, output_root)

            barrier.wait(timeout=2)
            relative_input = Path(task_config.problem_statement.spreadsheet_path).relative_to(
                "/mnt/spreadsheet_data"
            )
            input_bytes = (input_root / relative_input).read_bytes()
            (output_root / f"{task_id}_output.xlsx").write_bytes(input_bytes + b"-output")

        dataset = [
            {"id": "a", "instruction": "A", "spreadsheet_path": "spreadsheet/a.xlsx"},
            {"id": "b", "instruction": "B", "spreadsheet_path": "spreadsheet/b.xlsx"},
        ]
        output_base = self.temp_path / "published"

        def run_task(index: int, task_data: dict) -> Path:
            return run_single._run_spreadsheet_task(
                task_data=task_data,
                task_index=index,
                config=config,
                dataset_path=dataset_path,
                output_dir=self.temp_path / "trajectories",
                output_base=output_base,
                container_data_path="/mnt/spreadsheet_data",
                container_output_root="/mnt/spreadsheet_output",
            )

        with patch.object(run_single, "run_from_config", fake_run_from_config):
            completed, failed = run_single._execute_dataset_tasks(dataset, 2, run_task)

        self.assertEqual((completed, failed), (2, 0))
        self.assertEqual((output_base / "a_output.xlsx").read_bytes(), b"input-a-output")
        self.assertEqual((output_base / "b_output.xlsx").read_bytes(), b"input-b-output")
        self.assertNotEqual(seen_mounts["a"][0], seen_mounts["b"][0])
        self.assertNotEqual(seen_mounts["a"][1], seen_mounts["b"][1])

    def test_parallel_run_log_files_are_task_isolated(self) -> None:
        output_dir = self.temp_path / "logs"
        handlers_ready = threading.Barrier(2)
        messages_written = threading.Barrier(2)
        shared_logger = get_logger(f"spreadsheet-log-isolation-{id(self)}")

        def write_task_log(task_id: str) -> None:
            register_thread_name(task_id)
            task_run = run_single.RunSingle(
                env=object(),
                agent=object(),
                problem_statement=SimpleNamespace(id=task_id),
                output_dir=output_dir,
            )
            try:
                handlers_ready.wait(timeout=2)
                shared_logger.info("message-from-%s", task_id)
                messages_written.wait(timeout=2)
            finally:
                for handler_id in task_run._log_handler_ids:
                    run_single.remove_file_handler(handler_id)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="log-isolation") as executor:
            futures = [executor.submit(write_task_log, task_id) for task_id in ["log-a", "log-b"]]
            for future in futures:
                future.result(timeout=5)

        task_a_log = (output_dir / "log-a" / "log-a.info.log").read_text(encoding="utf-8")
        task_b_log = (output_dir / "log-b" / "log-b.info.log").read_text(encoding="utf-8")
        self.assertIn("message-from-log-a", task_a_log)
        self.assertNotIn("message-from-log-b", task_a_log)
        self.assertIn("message-from-log-b", task_b_log)
        self.assertNotIn("message-from-log-a", task_b_log)

    def test_executor_caps_concurrency_and_isolates_failures(self) -> None:
        dataset = [{"id": str(index)} for index in range(6)]
        lock = threading.Lock()
        active = 0
        max_active = 0

        def run_task(index: int, task_data: dict) -> Path:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                if index == 2:
                    raise RuntimeError("expected test failure")
                return self.temp_path / f"{task_data['id']}.xlsx"
            finally:
                with lock:
                    active -= 1

        completed, failed = run_single._execute_dataset_tasks(dataset, 3, run_task)

        self.assertEqual((completed, failed), (5, 1))
        self.assertEqual(max_active, 3)

    def test_num_workers_must_be_positive(self) -> None:
        field = RunSingleConfig.model_fields["num_workers"]

        self.assertEqual(field.default, 1)
        self.assertTrue(any(getattr(metadata, "ge", None) == 1 for metadata in field.metadata))

    def test_cli_parses_num_workers(self) -> None:
        config = BasicCLI(RunSingleConfig).get_config(
            [
                "--config",
                str(CONFIG_DIR / "spreadsheet.yaml"),
                "--agent.model.name",
                "openrouter/openai/gpt-5.6-sol",
                "--num_workers",
                "4",
            ]
        )

        self.assertEqual(config.num_workers, 4)

    def test_environment_close_timeout_force_removes_exact_container(self) -> None:
        container_name = "spreadsheetbench-v2-00000000-0000-0000-0000-000000000001"

        class HangingDockerDeployment(DockerDeployment):
            def __init__(self) -> None:
                self._container_name = container_name
                self._config = SimpleNamespace(container_runtime="docker")

            async def stop(self) -> None:
                await asyncio.Event().wait()

        environment = SWEEnv(
            deployment=HangingDockerDeployment(),
            repo=None,
            post_startup_commands=[],
        )
        completed_process = SimpleNamespace(returncode=0, stderr="")

        with (
            patch.object(swe_env_module, "_DEPLOYMENT_STOP_TIMEOUT", 0.01),
            patch.object(swe_env_module.subprocess, "run", return_value=completed_process) as run_command,
        ):
            environment.close()

        run_command.assert_called_once_with(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=swe_env_module._FORCE_REMOVE_TIMEOUT,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
