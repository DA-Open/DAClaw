"""Agent profiles (spec 1.1, 2, 5.2): who the agent is for a task.

``agents/`` is configuration and policy for the two roles, not a second
execution kernel: the same AgentRunner runs every profile. A task fixes its
profile by ``agent_profile_ref`` (``main@1``) when it is created, and resumes
with that same version (spec 3.1).

P0 ships the Main profile only. Proactive comes with P2.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ..contracts import AgentProfile, AgentRole, NotFound, RunLane

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class AgentRegistry:
    """``interfaces.AgentRegistry``: profiles by exact role and version."""

    def __init__(self, profiles: Iterable[AgentProfile] = ()) -> None:
        self._profiles: dict[tuple[str, str], AgentProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: AgentProfile) -> None:
        key = (str(profile.role), profile.version)
        if key in self._profiles:
            raise ValueError(f"profile {profile.ref} is registered twice")
        self._profiles[key] = profile

    def resolve(self, role: AgentRole | str, version: str) -> AgentProfile:
        try:
            return self._profiles[(str(role), str(version))]
        except KeyError:
            raise NotFound(f"agent profile {role}@{version} is not registered") from None

    def resolve_ref(self, ref: str) -> AgentProfile:
        """``main@1`` -> the profile."""
        role, _, version = ref.partition("@")
        return self.resolve(role, version)

    def tool_policies(self) -> dict[str, str]:
        """``profile ref -> tool_policy_ref`` for ``ToolGateway(profiles=...)``."""
        return {p.ref: p.tool_policy_ref for p in self._profiles.values()}

    @classmethod
    def builtin(cls) -> AgentRegistry:
        from .main import MAIN_PROFILE

        return cls([MAIN_PROFILE])


class PackageInstructions:
    """``context.builder.InstructionLoader`` reading prompt files shipped
    inside the harness package (``instructions_ref`` is a path relative to
    ``src/harness``, e.g. ``prompts/main.md``). Refs that escape the package
    are refused."""

    def __init__(self, root: Path | None = None, extra: dict[str, str] | None = None) -> None:
        self._root = (root or PACKAGE_ROOT).resolve()
        self._extra = dict(extra or {})

    async def __call__(self, instructions_ref: str) -> str:
        if instructions_ref in self._extra:
            return self._extra[instructions_ref]
        path = (self._root / instructions_ref).resolve()
        if self._root not in path.parents:
            raise KeyError(f"instructions {instructions_ref!r} are outside the harness package")
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise KeyError(f"no instructions for {instructions_ref!r}") from None


def main_role_whitelist() -> dict[str, frozenset[str]]:
    """Role whitelist entry for ``WhitelistPolicy(roles=...)``: the P0 tools
    the Main role may call. Users and capability packs can only narrow it."""
    from .main import MAIN_PROFILE, MAIN_TOOLS

    return {MAIN_PROFILE.tool_policy_ref: MAIN_TOOLS}


__all__ = ["AgentRegistry", "PackageInstructions", "main_role_whitelist", "RunLane"]
