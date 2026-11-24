"""Background data understanding; no access to Main conversation history."""
from ..contracts import AgentProfile, AgentRole, RunLane

PROACTIVE_TOOLS = frozenset({"data.profile", "data.connect", "data.query", "artifacts.read", "artifacts.write",
                             "memory.search", "memory.read"})
PROACTIVE_PROFILE = AgentProfile(
    role=AgentRole.PROACTIVE, version="1", instructions_ref="prompts/proactive.md",
    context_policy={"prefer": "subscription", "history": "none"},
    tool_policy_ref="proactive_tools@1", default_lane=RunLane.BACKGROUND,
    default_delivery_mode="digest",
)
