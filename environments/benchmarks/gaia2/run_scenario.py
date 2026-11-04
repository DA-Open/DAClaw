"""Run one GAIA2 scenario with DAClaw harness ③ and ARE's judge, in one process.

Flow (mirrors are.simulation.scenario_runner.ScenarioRunner._run + the
ARE default agent's outer loop, with the inner ReAct loop replaced by
DAClawAgentLoop):

1. load scenario from the HF row, ``preprocess_scenario_from_config`` (oracle
   run + GraphPerEventJudge with the configured judge model, turn triggers);
2. start the ARE Environment (time-based loop, VerboseNotificationSystem);
3. register the scenario's app tools in ``tools/registry.py``;
4. wake the agent whenever ARE has a due user message *or* environment
   notification (as ARE's default agent does), append it to the conversation
   and run ``DAClawAgentLoop`` until the agent calls
   ``AgentUserInterface__send_message_to_user``. If the model ends with plain
   text instead, it gets ARE's own invalid-action error message (up to
   ``agent.text_reply_nudges`` times, each counted against the call budget,
   as in ARE). ARE's turn trigger validates each turn online and either
   releases the next turn's events or stops the env;
5. ``scenario.validate(env)`` -> pass/fail + rationale; export ARE trace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from environments.benchmarks.gaia2 import are_bridge as B

logger = logging.getLogger("gaia2")


def build_system_prompt(scenario, notification_system) -> str:
    """ARE's environment instructions + notification policy + date, without
    the ReAct/JSON action-format section (tools are passed via tools=)."""
    from are.simulation.agents.default_agent.prompts.notification_system import (
        get_notification_system_prompt,
    )
    from are.simulation.agents.default_agent.prompts.system_prompt import (
        ARE_SIMULATION_ENVIRONMENT_INSTRUCTIONS,
        GENERAL_SYSTEM_PROMPT_TEMPLATE,
    )

    env_instr = ARE_SIMULATION_ENVIRONMENT_INSTRUCTIONS.format(environment_hints="")
    env_instr = env_instr.replace(
        "<<tool_descriptions>>",
        "The tools are provided to you through the function-calling interface. "
        "Tool names have the form <AppName>__<function>.",
    )
    env_instr = env_instr.replace(
        "<<notification_system_description>>",
        get_notification_system_prompt(notification_system, scenario.apps),
    )
    env_instr = env_instr.replace("<<agent_reminder_description>>", "")
    date_str = datetime.fromtimestamp(scenario.start_time or 0, tz=timezone.utc).strftime("%Y-%m-%d %H")
    env_instr = env_instr.replace(
        "<<curent_time_description>>", f"Today's date in 'YYYY-MM-DD HH' format is {date_str}"
    )
    prompt = (
        "<general_instructions>\n" + GENERAL_SYSTEM_PROMPT_TEMPLATE + "\n</general_instructions>\n\n"
        "<agent_instructions>\nUse the provided tools to solve the user's task. "
        "When the task is done (or impossible / needs clarification), call "
        f"{B.SEND_TO_USER} with your reply; that ends your turn.\n</agent_instructions>\n\n"
        "<environment_instructions>\n" + env_instr + "\n</environment_instructions>"
    )
    if scenario.additional_system_prompt:
        prompt += "\n\n" + scenario.additional_system_prompt
    return prompt


# ARE base_agent "error" history template; an ErrorLog renders its content as
# "Error: <type>\nException: <msg>\nCategory: <category>" (agent_log.py).
_ARE_ERROR_TEMPLATE = (
    "[OUTPUT OF STEP {i}] ERROR:\n***\n{content}\n***\n\n"
    "Now let's retry: take care not to repeat previous errors! "
    "If you have retried several times, try a completely different approach.\n"
)
_ARE_ERROR_PREFIX = "[OUTPUT OF STEP "


def invalid_action_message(messages: List[dict], text: str) -> dict:
    """What ARE's agent sees after an output with no valid action: an
    InvalidActionAgentError (category AgentError) rendered with ARE's error
    template. ARE says the output "did not contain Action: or Thought:"; with
    function calling the equivalent is that it contained no tool call.

    The step number follows ARE's counter, which advances on every
    observation (here: tool result) and every error.
    """
    step = 1 + sum(
        1 for m in messages
        if m.get("role") == "tool"
        or (m.get("role") == "user" and str(m.get("content", "")).startswith(_ARE_ERROR_PREFIX))
    )
    content = (
        "Error: InvalidActionAgentError\n"
        f"Exception: The LLM output was not formatted correctly: {text} - did not contain a tool call\n"
        "Category: AgentError"
    )
    return {"role": "user", "content": _ARE_ERROR_TEMPLATE.format(i=step, content=content)}


def _wait_for_wakeup(env, timeout_s: float):
    """Block until ARE has something for the agent: a user message, an
    environment notification (e.g. a new email), or the environment stopping.

    Mirrors ARESimulationAgent.agent_loop, which runs the agent on new user
    messages *and* on environment notifications.
    """
    from are.simulation.notification_system import MessageType

    wake_types = (MessageType.USER_MESSAGE, MessageType.ENVIRONMENT_NOTIFICATION,
                  MessageType.ENVIRONMENT_STOP)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        q = env.notification_system.message_queue
        now = datetime.fromtimestamp(env.time_manager.time(), tz=timezone.utc)
        if any(m.timestamp <= now and m.message_type in wake_types for m in q.list_view()):
            return True
        if env.state.value in ("STOPPED", "FAILED"):
            return False
        time.sleep(0.5)
    return False


def _last_text_reply(messages: List[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant":
            return (m.get("content") or "").strip() if not m.get("tool_calls") else ""
    return ""


async def run_scenario(row: Dict[str, Any], cfg: Dict[str, Any], out_dir: Path, client) -> Dict[str, Any]:
    from are.simulation.agents.are_simulation_agent_config import LLMEngineConfig
    from are.simulation.benchmark.scenario_loader import load_scenario
    from are.simulation.data_handler.exporter import JsonScenarioExporter
    from are.simulation.environment import Environment, EnvironmentConfig
    from are.simulation.notification_system import VerboseNotificationSystem
    from are.simulation.scenarios.config import ScenarioRunnerConfig
    from are.simulation.scenarios.scenario import ScenarioStatus
    from are.simulation.scenarios.scenario_imported_from_json.utils import (
        preprocess_scenario_from_config,
    )
    from are.simulation.types import EnvironmentType
    from environments.agent_loop import DAClawAgentLoop

    a, j, r = cfg["agent"], cfg["judge"], cfg["run"]
    sid = row["scenario_id"]
    # Judge-only patch (see are_bridge.patch_llm_checker); also records raw checker outputs.
    B.patch_llm_checker(bool(j.get("fix_case_bug", False)))
    B.JUDGE_LOG.clear()
    B.JUDGE_ERRORS.clear()
    t_start = time.time()
    out: Dict[str, Any] = {
        "scenario_id": sid, "hf_config": cfg["dataset"]["hf_config"],
        "category": row.get("category"), "harness": "DAClawAgentLoop (③)",
        "agent_model": a["model"], "judge_model": j["model"],
        "success": None, "rationale": None, "error": None,
    }
    tool_names: set = set()
    env = None
    try:
        scenario, _ = load_scenario(row["data"], sid, False)
        runner_cfg = ScenarioRunnerConfig(
            model=a["model"], model_provider="local", agent="daclaw_agent_loop",
            endpoint=a["base_url"], export=True, output_dir=str(out_dir),
            judge_engine_config=LLMEngineConfig(
                model_name=j["model"], provider=j["provider"], endpoint=j["endpoint"]),
        )
        preprocess_scenario_from_config(scenario, runner_cfg)
        out["nb_turns"] = scenario.nb_turns
        out["tags"] = [str(getattr(t, "value", t)) for t in (scenario.tags or [])]

        env_cfg = EnvironmentConfig(
            oracle_mode=False, queue_based_loop=False,
            time_increment_in_seconds=scenario.time_increment_in_seconds,
            exit_when_no_events=False,
        )
        if scenario.start_time and scenario.start_time > 0:
            env_cfg.start_time = scenario.start_time
        env = Environment(environment_type=EnvironmentType.CLI, config=env_cfg,
                          notification_system=VerboseNotificationSystem())
        env.run(scenario, wait_for_end=False)

        # Same AUI setting as ARE's default agent.
        aui = env.get_app("AgentUserInterface")
        aui.wait_for_user_response = False

        tracker = B.ToolCallTracker()
        tools, tool_names = B.register_scenario_tools(scenario, tracker)
        out["n_tools"] = len(tool_names)
        server = B.AREChatServer(client, a["model"], env, tracker)

        messages: List[dict] = [{"role": "system", "content": build_system_prompt(scenario, env.notification_system)}]
        turns: List[dict] = []
        # ARE outer loop: a "turn" ends when the agent calls send_message_to_user
        # (ARE RunningState.TERMINATED); the agent is (re)started whenever a user
        # message or an environment notification is due; stop after nb_turns
        # completed turns or when the environment stops.
        n_turns = scenario.nb_turns or 1
        max_nudges = int(a.get("text_reply_nudges", 10))
        forward_text = bool(a.get("forward_text_reply", False))
        completed, run_idx = 0, 0
        while completed < n_turns:
            if not _wait_for_wakeup(env, r["turn_wait_timeout"]):
                turns.append({"turn": completed, "skipped": "nothing to wake the agent / env stopped"})
                break
            users, notifs, stop = B.drain_notifications(env)
            if stop:
                turns.append({"turn": completed, "skipped": "environment stopped"})
                break
            messages.extend(B.notification_messages(users, notifs))
            tracker.sent_to_user_in_turn = False
            t0 = time.time()
            info = {"turn": completed, "run": run_idx,
                    "woken_by": "user" if users else "notification",
                    "nudges": 0, "loop_turns": 0}
            budget = a["max_agent_turns"]
            while True:
                loop = DAClawAgentLoop(
                    server=server, tool_schemas=tools, valid_tool_names=tool_names,
                    max_turns=budget, task_id=f"gaia2-{sid}-{run_idx}",
                    temperature=a["agent_temperature"], max_tokens=a.get("max_token_length"),
                    extra_body=a.get("extra_body"),
                )
                res = await loop.run(messages)
                info["loop_turns"] += res.turns_used
                budget -= res.turns_used
                # Drop the synthetic empty assistant message the adapter returned
                # to end the loop after send_message_to_user / env stop.
                if (messages and messages[-1].get("role") == "assistant"
                        and not messages[-1].get("content") and not messages[-1].get("tool_calls")
                        and (tracker.sent_to_user_in_turn or server.env_stopped)):
                    messages.pop()
                if tracker.sent_to_user_in_turn or server.env_stopped or server.api_error:
                    break
                if not res.finished_naturally or budget <= 0:
                    info["hit_limit"] = True
                    break
                # Model ended with a plain-text reply instead of a tool call.
                # ARE's own agent logs an InvalidActionAgentError, shows it with
                # its error template and continues; the failed call counts as an
                # iteration (base_agent.execute_agent_loop increments
                # `iterations` in `finally`), as it does here via `budget`.
                if info["nudges"] < max_nudges:
                    info["nudges"] += 1
                    messages.append(invalid_action_message(messages, _last_text_reply(messages)))
                    continue
                info["text_reply_without_send"] = True
                break
            info.update({
                "llm_calls_total": server.llm_calls, "sent_to_user": tracker.sent_to_user_in_turn,
                "seconds": round(time.time() - t0, 1),
            })
            turns.append(info)
            run_idx += 1
            if server.api_error:
                # Agent LLM unreachable after retries: not the model's answer, stop here.
                info["api_error"] = server.api_error
                break
            if server.env_stopped:
                break
            if not tracker.sent_to_user_in_turn:
                if info.get("text_reply_without_send") and forward_text and _last_text_reply(messages):
                    # Deliver the model's own final text as its message to the user.
                    aui.send_message_to_user(content=_last_text_reply(messages))
                    info["forwarded_text_reply"] = True
                elif info.get("hit_limit"):
                    # ARE's max-iterations behaviour: auto message to the user.
                    aui.send_message_to_user(content=f"Max iterations ({a['max_agent_turns']}) reached. Stopping.")
                    info["auto_send_on_limit"] = True
                else:
                    # Agent ended a run without replying (e.g. after
                    # wait_for_notification); not a completed turn, wait again.
                    continue
            completed += 1
            # Let the turn trigger (online judge for this turn) run.
            time.sleep(2)
        out["completed_turns"] = completed

        # Wait for the final turn's trigger / env to settle, then validate.
        time.sleep(3)
        vr = scenario.validate(env)
        out["success"] = bool(vr.success) if vr.success is not None else False
        out["rationale"] = vr.rationale
        if vr.exception is not None:
            out["error"] = f"{type(vr.exception).__name__}: {vr.exception}"
        if server.api_error:
            out["error"] = f"agent_api_error: {server.api_error}"
        out["turns"] = turns
        out["llm_calls"] = server.llm_calls
        out["tool_calls"] = tracker.calls
        out["tool_call_errors"] = tracker.errors
        out["usage"] = server.usage
        out["api_seconds"] = round(server.api_seconds, 1)
        out["sim_time_passed_s"] = env.time_manager.time_passed()
        out["env_state"] = env.state.value

        exporter = JsonScenarioExporter()
        ok, path = exporter.export_to_json_file(
            env, scenario, a["model"], "daclaw_agent_loop",
            ScenarioStatus.Valid.value if out["success"] else ScenarioStatus.Invalid.value,
            vr.rationale, run_duration=env.time_manager.time_passed(),
            output_dir=str(out_dir / "are_trace"), export_apps=False, trace_dump_format="hf",
            scenario_exception=vr.exception, runner_config=runner_cfg,
        )
        out["are_trace_path"] = path if ok else None
    except Exception as e:
        logger.exception("scenario %s failed", sid)
        out["success"] = False
        out["error"] = f"{type(e).__name__}: {e}"
        messages = locals().get("messages", [])
    finally:
        if tool_names:
            B.deregister_tools(tool_names)
        if env is not None:
            try:
                env.stop()
            except Exception:
                pass
    # status: ok = agent ran and the judge scored it; error = infra/judge
    # failure, `success` is not a model result and the scenario should be rerun.
    out["status"] = "error" if out.get("error") else "ok"
    if out["status"] == "error":
        out["success"] = None
    out["judge_fix_case_bug"] = bool(j.get("fix_case_bug", False))
    out["judge_log"] = [{k: v for k, v in e.items() if k != "response"} for e in B.JUDGE_LOG]
    out["judge_call_retries"] = list(B.JUDGE_ERRORS)
    out["elapsed_seconds"] = round(time.time() - t_start, 1)
    (out_dir / "messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=1, default=str))
    out["messages_path"] = str(out_dir / "messages.json")
    (out_dir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return out
