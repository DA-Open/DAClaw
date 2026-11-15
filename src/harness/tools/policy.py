"""Who may call which tool on what (spec 6.2 step 2, spec 7).

P0 has no grants table. Two whitelists from config stand in for it, as
decided in the review of #12:

* **Role whitelist** -- which tools each agent profile may call
  (``AgentProfile.tool_policy_ref``). This is the floor: the proactive
  profile, for example, gets read-only tools only.
* **User whitelist** -- per ``scope_id`` and role, which tools and which
  resources (file paths) that user's agent may touch. It covers several users
  sharing one deployment, e.g. one Feishu bot serving many people.

A call is allowed only by the intersection of both, plus what the capability
pack needs. A user entry can only narrow its role list, never widen it. When
the grants table arrives (P1) it replaces the user whitelist; this module's
interface stays the same.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..contracts import AgentProfile, AgentRole, ExecutionContext, Task, ToolSpec


@dataclass(frozen=True)
class UserGrant:
    """What one user allows one role to do.

    ``tools`` narrows the role whitelist; ``None`` means "everything the role
    allows". ``paths`` are directories (or files) the file tools may read;
    each is matched as a path prefix after resolving.
    """

    tools: frozenset[str] | None = None
    paths: tuple[str, ...] = ()
    ref: str | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    grant_ref: str | None = None


ALLOW = Decision(True)


def _deny(reason: str) -> Decision:
    return Decision(False, reason)


@dataclass
class WhitelistPolicy:
    """Role whitelist ∩ user whitelist ∩ capability needs.

    ``roles`` maps ``AgentProfile.tool_policy_ref`` to tool names.
    ``users`` maps ``scope_id`` -> role -> ``UserGrant``. A scope with no
    entry gets nothing: access is opt-in per user.
    ``capabilities`` maps ``Task.capability_ref`` to the tools that pack needs
    (spec 7: "能力包里的 tools 是需要使用哪些工具，不是授权"); a capability with
    no entry does not narrow anything.
    ``disabled_tools`` is the deployment policy (spec 7: the fourth layer of
    the intersection): tools an operator turned off for the whole deployment,
    whatever any role, user or pack says.
    """

    roles: Mapping[str, Iterable[str]]
    users: Mapping[str, Mapping[AgentRole | str, UserGrant]] = field(default_factory=dict)
    capabilities: Mapping[str, Iterable[str]] = field(default_factory=dict)
    disabled_tools: Iterable[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self.roles = {k: frozenset(v) for k, v in self.roles.items()}
        self.capabilities = {k: frozenset(v) for k, v in self.capabilities.items()}
        self.disabled_tools = frozenset(self.disabled_tools)
        self.users = {
            scope: {str(role): grant for role, grant in by_role.items()}
            for scope, by_role in self.users.items()
        }

    # -- tool level ---------------------------------------------------------

    def role_tools(self, profile_policy_ref: str) -> frozenset[str]:
        return frozenset(self.roles.get(profile_policy_ref, ()))

    def user_grant(self, scope_id: str, role: AgentRole | str) -> UserGrant | None:
        return self.users.get(scope_id, {}).get(str(role))

    def allowed_tools(
        self, *, scope_id: str, role: AgentRole | str, tool_policy_ref: str, capability_ref: str | None
    ) -> frozenset[str]:
        tools = self.role_tools(tool_policy_ref)
        grant = self.user_grant(scope_id, role)
        if grant is None:
            return frozenset()
        if grant.tools is not None:
            tools &= grant.tools
        if capability_ref is not None and capability_ref in self.capabilities:
            tools &= self.capabilities[capability_ref]
        return tools - self.disabled_tools

    def check_tool(
        self,
        tool: str,
        *,
        scope_id: str,
        role: AgentRole | str,
        tool_policy_ref: str,
        capability_ref: str | None,
    ) -> Decision:
        if tool in self.disabled_tools:
            return _deny(f"tool {tool!r} is disabled in this deployment")
        if tool not in self.role_tools(tool_policy_ref):
            return _deny(f"tool {tool!r} is not allowed for this agent role")
        grant = self.user_grant(scope_id, role)
        if grant is None:
            return _deny("this user has not granted the agent any tools")
        if grant.tools is not None and tool not in grant.tools:
            return _deny(f"tool {tool!r} is not granted by this user")
        if capability_ref is not None and capability_ref in self.capabilities:
            if tool not in self.capabilities[capability_ref]:
                return _deny(f"tool {tool!r} is not part of capability {capability_ref}")
        return Decision(True, grant_ref=grant.ref)

    # -- resource level -----------------------------------------------------

    def check_path(self, path: Path, *, scope_id: str, role: AgentRole | str, extra: Sequence[Path] = ()) -> Decision:
        """``path`` must already be resolved. ``extra`` are further allowed
        roots for this call, e.g. the task's own input files."""
        grant = self.user_grant(scope_id, role)
        roots = [Path(p).resolve() for p in (grant.paths if grant else ())]
        roots += [Path(p).resolve() for p in extra]
        for root in roots:
            if path == root or root in path.parents:
                return Decision(True, grant_ref=grant.ref if grant else None)
        return _deny("path is outside the directories this user allows")


@dataclass(frozen=True)
class ToolPolicyContext:
    """The inputs a policy check needs, taken from the execution and task."""

    scope_id: str
    role: AgentRole
    tool_policy_ref: str
    capability_ref: str | None

    @classmethod
    def of(cls, execution: ExecutionContext, task: Task, profile_policy_ref: str) -> ToolPolicyContext:
        return cls(
            scope_id=execution.actor.scope_id,
            role=execution.agent_role,
            tool_policy_ref=profile_policy_ref,
            capability_ref=task.capability_ref,
        )


def input_file_paths(task: Task) -> list[Path]:
    """Paths the task was explicitly given (``input_refs`` of the form
    ``file:<path>``). The file tools may read these even when they sit outside
    the user's whitelisted directories: the user named them in the request."""
    out = []
    for ref in task.input_refs:
        if ref.startswith("file:"):
            raw = ref[len("file:"):]
            if raw.startswith("//"):
                raw = str(PurePosixPath(raw[2:]))
            out.append(Path(raw).expanduser())
    return out


class PolicyToolCatalog:
    """``context.builder.ToolCatalog`` backed by a policy: the model is shown
    exactly the tools it may call (spec 7: 实际可用工具取交集)."""

    def __init__(self, policy: WhitelistPolicy, specs: Sequence[ToolSpec]) -> None:
        self._policy = policy
        self._specs = list(specs)

    async def tools_for(self, task: Task, profile: AgentProfile) -> list[ToolSpec]:
        allowed = self._policy.allowed_tools(
            scope_id=task.scope_id,
            role=task.agent_role,
            tool_policy_ref=profile.tool_policy_ref,
            capability_ref=task.capability_ref,
        )
        return [s for s in self._specs if s.name in allowed]
