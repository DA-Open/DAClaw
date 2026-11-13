"""Tool catalogs handed to the ContextBuilder (spec 7 step 2).

``ContextBuilder`` lists the tools it is given and adds nothing, so the
catalog it receives must already be filtered by the policy: role whitelist ∩
user whitelist. ``PolicyToolCatalog`` does that; ``StaticToolCatalog`` is the
unfiltered list used by tests that only exercise other modules.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..contracts import AgentProfile, Task, ToolSpec
from ..interfaces import Connector
from .policy import ToolPolicy


class StaticToolCatalog:
    def __init__(self, tools: Iterable[ToolSpec]) -> None:
        self._tools = list(tools)

    async def tools_for(self, task: Task, profile: AgentProfile) -> list[ToolSpec]:
        return list(self._tools)


class PolicyToolCatalog:
    def __init__(self, tools: Iterable[ToolSpec], policy: ToolPolicy) -> None:
        self._specs: dict[str, ToolSpec] = {tool.name: tool for tool in tools}
        self._policy = policy

    async def tools_for(self, task: Task, profile: AgentProfile) -> list[ToolSpec]:
        allowed = self._policy.allowed_tools(task.agent_role, task.scope_id)
        return [spec for name, spec in self._specs.items() if name in allowed]

    def spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    @classmethod
    def from_connectors(
        cls, connectors: Mapping[str, Connector], policy: ToolPolicy
    ) -> PolicyToolCatalog:
        return cls((connector.spec for connector in connectors.values()), policy)
