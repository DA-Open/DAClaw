"""Tool permissions: role whitelist ∩ user whitelist (spec 3.2, 7).

P0 has no ``grants`` table. Two whitelists stand in:

* the **role** whitelist is the floor: which tools Main / Proactive may call
  at all;
* the **user** whitelist narrows that floor for one ``(scope_id, role)``, so
  several users of one deployment (say one Feishu bot) can differ. It can only
  remove tools; anything it names outside the role list has no effect.

The effective set is their intersection. A user with no entry gets the role
list unchanged, because "no entry" means "not configured yet", not "nothing".
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ..contracts import AgentRole


class PolicyDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class ToolPolicy:
    # Keys are role values ("main", "proactive").
    role_tools: Mapping[str, Collection[str]] = field(default_factory=dict)
    # Keys are (scope_id, role value).
    user_tools: Mapping[tuple[str, str], Collection[str]] = field(default_factory=dict)
    approval_required: frozenset[str] = frozenset()

    def allowed_tools(self, role: AgentRole | str, scope_id: str) -> frozenset[str]:
        role_key = str(role)
        allowed = frozenset(self.role_tools.get(role_key, ()))
        user = self.user_tools.get((scope_id, role_key))
        if user is not None:
            allowed = allowed & frozenset(user)
        return allowed

    def authorize(self, role: AgentRole | str, scope_id: str, tool: str) -> PolicyDecision:
        if tool not in self.allowed_tools(role, scope_id):
            return PolicyDecision.DENIED
        if tool in self.approval_required:
            return PolicyDecision.NEEDS_APPROVAL
        return PolicyDecision.ALLOWED
