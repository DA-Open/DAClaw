"""Bridge between ARE (Meta Agents Research Environments) and DAClaw harness ③.

- ``ensure_are_importable``: ARE lives in a ``--no-deps --target`` directory
  (``are_site``) that is *appended* to ``sys.path`` so every package already in
  the DAClaw venv keeps its pinned version.
- ``register_scenario_tools``: turns each ARE ``AppTool`` of the running
  scenario into a DAClaw ``tools/registry.py`` entry whose handler calls the
  app method directly. ARE's ``@event_registered`` decorator therefore logs the
  call as an AGENT event in the same process, which is what the judge reads.
- ``AREChatServer``: the ``server`` object given to ``DAClawAgentLoop``. It
  wraps the OpenAI-compatible endpoint and reproduces the two per-step hooks of
  ARE's default agent: pull notifications from the environment before each LLM
  call, and pause the simulated clock while the model is generating.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

TOOLSET = "gaia2-are"
SEND_TO_USER = "AgentUserInterface__send_message_to_user"
# Same removal as ARE's ARESimulationAgent.remove_aui_irrelevant_tools: user
# messages are pushed to the agent as notifications instead.
AUI_TOOLS_REMOVED = {
    "AgentUserInterface__get_last_message_from_user",
    "AgentUserInterface__get_last_message_from_agent",
    "AgentUserInterface__get_last_unread_messages",
    "AgentUserInterface__get_all_messages",
}
# History templates copied from ARE base_agent (role=user for both).
USER_TEMPLATE = "User messages updates:\n***\n{content}\n***\n"
NOTIF_TEMPLATE = "Environment notifications updates:\n***\n{content}\n***\n"


def ensure_are_importable(are_site: str) -> None:
    if are_site not in sys.path:
        sys.path.append(are_site)


# ---------------------------------------------------------------------------
# Judge instrumentation / fix
# ---------------------------------------------------------------------------
# ARE 1.2.0 LiteLLMEngine.chat_completion (litellm_engine.py:125) rewrites
# "True"->"true" / "False"->"false" in every output, but LLMChecker matches the
# literal success_str "[[True]]" (signature/sanity/tone checkers, llm_utils.py),
# so those checkers return None and SoftToolJudge.compare() treats it as reject.
# The patch below replaces LLMChecker.__call__ (used only by the judge) with the
# same voting logic that (a) records every raw response and (b) when
# ``fix_case`` is True matches success/failure strings case-insensitively.

JUDGE_LOG: List[dict] = []
JUDGE_ERRORS: List[str] = []
_CHECKER_NAMES: Dict[int, str] = {}


def patch_llm_checker(fix_case: bool) -> None:
    from are.simulation.validation.utils import llm_utils

    checker_names = _CHECKER_NAMES
    orig_build = llm_utils.build_llm_checkers
    if not getattr(orig_build, "_gaia2_wrapped", False):
        def build_llm_checkers(*a, **kw):
            d = orig_build(*a, **kw)
            for name, ch in d.items():
                checker_names[id(ch)] = name
            return d
        build_llm_checkers._gaia2_wrapped = True
        llm_utils.build_llm_checkers = build_llm_checkers
        # tool_judge imported the name directly
        from are.simulation.validation import tool_judge
        if hasattr(tool_judge, "build_llm_checkers"):
            tool_judge.build_llm_checkers = build_llm_checkers

    def _call_with_retry(fn, args, attempts=6):
        import time as _t
        for k in range(attempts):
            try:
                return fn(args)
            except Exception as e:  # network errors surface as LLMEngineException
                JUDGE_ERRORS.append(f"{type(e).__name__}: {e}")
                if k == attempts - 1:
                    raise
                _t.sleep(10 * (k + 1))

    if not getattr(llm_utils.LLMFunction.__call__, "_gaia2_retry", False):
        _orig_fn_call = llm_utils.LLMFunction.__call__

        def _fn_call(self, user_prompt_args):
            return _call_with_retry(lambda a: _orig_fn_call(self, a), user_prompt_args)
        _fn_call._gaia2_retry = True
        llm_utils.LLMFunction.__call__ = _fn_call

    def __call__(self, user_prompt_args):
        votes = []
        for _ in range(self.num_votes):
            response = self.judge(user_prompt_args)
            verdict = None
            if response is not None:
                hay = response.lower() if fix_case else response
                ok = self.success_str.lower() if fix_case else self.success_str
                bad = self.failure_str.lower() if fix_case else self.failure_str
                if ok in hay:
                    verdict = True
                elif bad in hay:
                    verdict = False
                if verdict is not None:
                    votes.append(verdict)
            JUDGE_LOG.append({
                "checker": checker_names.get(id(self), "?"),
                "success_str": self.success_str, "fix_case": fix_case,
                "verdict": verdict,
                "evaluation_tail": (response or "")[-300:],
                "response": response,
            })
        if not votes:
            return None
        return sum(votes) >= len(votes) / 2

    llm_utils.LLMChecker.__call__ = __call__


# ---------------------------------------------------------------------------
# Tool schema conversion
# ---------------------------------------------------------------------------

_SCALARS = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


def _split_union(t: str) -> List[str]:
    parts, depth, cur = [], 0, ""
    for ch in t:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return [p for p in parts if p not in ("None", "NoneType")]


def json_schema_for_type(t: Optional[str]) -> Dict[str, Any]:
    """Map ARE's python type strings (e.g. ``list[str] | None``) to JSON schema."""
    if not t:
        return {}
    parts = _split_union(str(t))
    schemas = []
    for p in parts:
        base = p.split("[", 1)[0].strip().lower()
        inner = p[p.index("[") + 1 : p.rindex("]")] if "[" in p and p.endswith("]") else ""
        if base in _SCALARS:
            schemas.append({"type": _SCALARS[base]})
        elif base in ("list", "tuple", "set"):
            item = json_schema_for_type(_split_union(inner)[0]) if inner else {}
            schemas.append({"type": "array", "items": item or {}})
        elif base == "dict":
            schemas.append({"type": "object"})
        # Any / unknown -> unconstrained
    if not schemas:
        return {}
    types = {s["type"] for s in schemas}
    if types == {"integer", "number"}:
        return {"type": "number"}
    return schemas[0]


def app_tool_to_schema(tool) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    required: List[str] = []
    for arg in tool.args:
        s = dict(json_schema_for_type(arg.arg_type))
        desc = arg.description or ""
        if arg.has_default:
            desc = f"{desc} (default: {arg.default!r})".strip()
        else:
            required.append(arg.name)
        if desc:
            s["description"] = desc
        props[arg.name] = s
    description = tool._public_description or tool.function_description or ""
    if tool.return_description:
        description += f"\nReturns: {tool.return_description}"
    return {
        "name": tool._public_name or tool.name,
        "description": description,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


# ---------------------------------------------------------------------------
# Registry bridge
# ---------------------------------------------------------------------------


class ToolCallTracker:
    """Per-scenario bookkeeping read by the server adapter and the runner."""

    def __init__(self):
        self.calls = 0
        self.errors = 0
        self.sent_to_user_in_turn = False


def register_scenario_tools(scenario, tracker: ToolCallTracker) -> Tuple[List[dict], Set[str]]:
    from tools.registry import registry

    app_tools = [t for t in scenario.get_tools() if t.name not in AUI_TOOLS_REMOVED]
    names: Set[str] = set()
    for tool in app_tools:
        schema = app_tool_to_schema(tool)
        name = schema["name"]

        def handler(args: dict, _tool=tool, _name=name, **_kw) -> str:
            tracker.calls += 1
            try:
                result = _tool(**(args or {}))
            except Exception as e:  # ARE shows the exception to the agent
                tracker.errors += 1
                return f"Error: {type(e).__name__}: {e}"
            if _name == SEND_TO_USER:
                tracker.sent_to_user_in_turn = True
            return str(result)

        registry.register(name=name, toolset=TOOLSET, schema=schema, handler=handler)
        names.add(name)
    schemas = registry.get_definitions(names, quiet=True)
    return schemas, names


def deregister_tools(names: Set[str]) -> None:
    from tools.registry import registry

    for n in names:
        registry.deregister(n)


# ---------------------------------------------------------------------------
# Server adapter for DAClawAgentLoop
# ---------------------------------------------------------------------------


def _finish_response(content: str = "") -> SimpleNamespace:
    msg = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


def drain_notifications(env) -> Tuple[List[str], List[str], bool]:
    """Pull due messages from ARE's notification queue (ARE pre-step)."""
    from are.simulation.notification_system import MessageType

    now = datetime.fromtimestamp(env.time_manager.time(), tz=timezone.utc)
    msgs = env.notification_system.message_queue.get_by_timestamp(now)
    users = [m.message for m in msgs if m.message_type == MessageType.USER_MESSAGE]
    notifs = [
        f"[{m.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] {m.message}"
        for m in msgs
        if m.message_type == MessageType.ENVIRONMENT_NOTIFICATION
    ]
    stop = any(m.message_type == MessageType.ENVIRONMENT_STOP for m in msgs)
    return users, notifs, stop


def notification_messages(users: List[str], notifs: List[str]) -> List[dict]:
    out = []
    if users:
        out.append({"role": "user", "content": USER_TEMPLATE.format(content="\n".join(users))})
    if notifs:
        out.append({"role": "user", "content": NOTIF_TEMPLATE.format(content="\n".join(notifs))})
    return out


class AREChatServer:
    """``server`` for DAClawAgentLoop: OpenAI chat completions + ARE hooks.

    Matches the call made by DAClawAgentLoop (``chat_completion(messages=...,
    n=1, temperature=..., tools=..., max_tokens=..., extra_body=...)``) and
    returns the OpenAI ChatCompletion object unchanged.
    """

    def __init__(self, client, model: str, env, tracker: ToolCallTracker,
                 max_retries: int = 4):
        self.client = client
        self.model = model
        self.env = env
        self.tracker = tracker
        self.max_retries = max_retries
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0,
                      "reasoning_tokens": 0, "cache_hit_tokens": 0}
        self.llm_calls = 0
        self.api_seconds = 0.0
        self.env_stopped = False
        self.notifications_seen = 0
        # Set when the agent LLM call fails after all retries (or returns no
        # choices). DAClawAgentLoop swallows that and just returns, so the
        # runner must check this to avoid scoring an infra failure as a FAIL.
        self.api_error: Optional[str] = None

    async def chat_completion(self, messages: List[dict], **kwargs):
        # ARE termination step: a send_message_to_user call ends the turn.
        if self.tracker.sent_to_user_in_turn:
            return _finish_response()
        # ARE pre-step: inject due user messages / environment notifications.
        users, notifs, stop = drain_notifications(self.env)
        if stop or self.env.state.value in ("STOPPED", "FAILED"):
            self.env_stopped = True
            return _finish_response()
        new = notification_messages(users, notifs)
        self.notifications_seen += len(users) + len(notifs)
        messages.extend(new)

        kwargs.pop("n", None)
        extra_body = kwargs.pop("extra_body", None)
        if extra_body:
            kwargs["extra_body"] = extra_body
        # ARE simulated_generation_time: pause the sim clock while generating.
        # (ARE's LiteLLM engine reports no completion_duration, so the official
        # run resumes with a 0s offset too.)
        self.env.pause()
        try:
            last_err = None
            for attempt in range(self.max_retries):
                t0 = time.monotonic()
                try:
                    resp = await self.client.chat.completions.create(
                        model=self.model, messages=messages, **kwargs)
                    self.api_seconds += time.monotonic() - t0
                    break
                except Exception as e:
                    last_err = e
                    logger.warning("LLM call failed (attempt %d): %s", attempt + 1,
                                   type(e).__name__)
                    await asyncio.sleep(5 * (attempt + 1))
            else:
                self.api_error = f"{type(last_err).__name__}: {last_err}"[:500]
                raise last_err
        finally:
            self.env.resume_with_offset(0.0)
        if not getattr(resp, "choices", None):
            self.api_error = "empty response (no choices)"
        self.llm_calls += 1
        u = getattr(resp, "usage", None)
        if u is not None:
            self.usage["prompt_tokens"] += u.prompt_tokens or 0
            self.usage["completion_tokens"] += u.completion_tokens or 0
            det = getattr(u, "completion_tokens_details", None)
            self.usage["reasoning_tokens"] += (getattr(det, "reasoning_tokens", 0) or 0) if det else 0
            self.usage["cache_hit_tokens"] += getattr(u, "prompt_cache_hit_tokens", 0) or 0
        return resp
