"""The Main agent profile (spec 1.1): the user's current request.

Main works in the foreground by default (a long task may run on the
background lane and is still ``main``), replies in the current conversation,
and may use the P0 tools: read files it is allowed to, and read / write this
task's artifacts. What it may actually call is this list intersected with the
user's whitelist and the task's capability pack.
"""

from __future__ import annotations

from ..contracts import AgentProfile, AgentRole, RunLane

MAIN_TOOLS = frozenset(
    {
        "files.read", "artifacts.read", "artifacts.write", "background_results.read", "background_results.mention",
        "data.list", "data.connect", "data.query", "feishu.send_message",
    }
)

MAIN_PROFILE = AgentProfile(
    role=AgentRole.MAIN,
    version="1",
    instructions_ref="prompts/main.md",
    context_policy={"prefer": "current_request", "history": "current_task"},
    tool_policy_ref="main_tools@1",
    default_lane=RunLane.FOREGROUND,
    default_delivery_mode="reply",
)
