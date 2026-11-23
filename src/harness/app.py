"""Wire the harness modules into one runnable system.

``build_system`` is the single place where repositories, artifact store,
policy, gateway, verifier, context builder, runner, task service and worker
are put together, so the worker CLI, the API (item 7) and the tests all get
the same wiring (README "Verification, capability packs, profiles").

Permissions come from a YAML file (``DARAOS_HARNESS_POLICY_FILE``)::

    disabled_tools: []            # deployment policy: off for everyone
    users:                        # the user whitelist, per scope_id
      s1:
        main:
          paths: [/data/s1]       # directories files.read may read
          tools: [files.read, artifacts.read, artifacts.write]   # optional, narrows
          ref: grant:s1:main      # optional, recorded on each action
    tokens:                       # API callers (item 7); see api/auth.py
      - sha256: 9f86d0...         # sha256 of the bearer token, never the token
        scope_id: s1
        actor_id: u1

The role whitelist comes from the agent profiles and the capability tool
lists from the packs; the file only says what each user allows. A scope with
no entry gets no tools (spec: access is opt-in per user).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .agents import AgentRegistry, PackageInstructions, main_role_whitelist
from .capabilities import CapabilityRegistry
from .config import HarnessConfig
from .context.builder import SimpleContextBuilder
from .context.resume import ResumingContextBuilder
from .contracts import AgentRole, RunLane, utcnow
from .interfaces import ModelAdapter
from .results import CapabilityVerifier, FileArtifactStore
from .runtime.runner import AgentRunner
from .storage.memory import InMemoryRepositories
from .storage.repositories import Repositories
from .tasks import TaskService, Worker
from .tools import PolicyToolCatalog, ToolGateway, UserGrant, WhitelistPolicy
from .tools.connectors import DataConnector, data_connectors, p0_connectors
from .tools.connectors.background_results import BackgroundResultsConnector
from .tools.connectors.data_profile import DataProfileConnector
from .results.checks.data_card import DataCardChecks
from .results.verifier import DEFAULT_CHECKERS
from .delivery.service import DeliveryService
from .tasks.proactive_controller import ProactiveController
from .tasks.recovery import Recovery
from .tasks.scheduler import Scheduler

if TYPE_CHECKING:
    from .storage.postgres import PgRepositories

_POLICY_KEYS = {"disabled_tools", "users", "tokens"}
_GRANT_KEYS = {"paths", "tools", "ref"}
_TOKEN_KEYS = {"sha256", "scope_id", "actor_id"}


class PolicyFileError(ValueError):
    """The permissions file is malformed."""


def load_policy_file(path: Path | str | None) -> dict[str, Any]:
    """Read and check the permissions YAML. ``None`` means no users at all."""
    if path is None:
        return {"disabled_tools": [], "users": {}, "tokens": []}
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise PolicyFileError(f"policy file {p} not found") from None
    except yaml.YAMLError as exc:
        raise PolicyFileError(f"{p.name}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise PolicyFileError(f"{p.name}: expected a mapping at the top level")
    unknown = set(data) - _POLICY_KEYS
    if unknown:
        raise PolicyFileError(f"{p.name}: unknown keys {sorted(unknown)}")
    disabled = data.get("disabled_tools") or []
    if not isinstance(disabled, list) or not all(isinstance(t, str) for t in disabled):
        raise PolicyFileError(f"{p.name}: disabled_tools must be a list of tool names")
    users = data.get("users") or {}
    if not isinstance(users, dict):
        raise PolicyFileError(f"{p.name}: users must map scope_id to roles")
    for scope, by_role in users.items():
        if not isinstance(by_role, dict):
            raise PolicyFileError(f"{p.name}: users.{scope} must map role to a grant")
        for role, grant in by_role.items():
            try:
                AgentRole(role)
            except ValueError:
                raise PolicyFileError(f"{p.name}: users.{scope}: unknown role {role!r}") from None
            if not isinstance(grant, dict) or set(grant) - _GRANT_KEYS:
                raise PolicyFileError(f"{p.name}: users.{scope}.{role} allows only {sorted(_GRANT_KEYS)}")
            for key in ("paths", "tools"):
                value = grant.get(key)
                if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
                    raise PolicyFileError(f"{p.name}: users.{scope}.{role}.{key} must be a list of strings")
    tokens = data.get("tokens") or []
    if not isinstance(tokens, list):
        raise PolicyFileError(f"{p.name}: tokens must be a list")
    seen: set[str] = set()
    for i, entry in enumerate(tokens):
        if not isinstance(entry, dict) or set(entry) != _TOKEN_KEYS:
            raise PolicyFileError(f"{p.name}: tokens[{i}] needs exactly {sorted(_TOKEN_KEYS)}")
        if not all(isinstance(entry[k], str) and entry[k] for k in _TOKEN_KEYS):
            raise PolicyFileError(f"{p.name}: tokens[{i}] values must be non-empty strings")
        digest = entry["sha256"].lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise PolicyFileError(f"{p.name}: tokens[{i}].sha256 must be 64 hex characters (the token's sha256)")
        if digest in seen:
            raise PolicyFileError(f"{p.name}: tokens[{i}] repeats a token")
        seen.add(digest)
    return {"disabled_tools": disabled, "users": users, "tokens": tokens}


def user_grants(spec: Mapping[str, Any]) -> dict[str, dict[str, UserGrant]]:
    out: dict[str, dict[str, UserGrant]] = {}
    for scope, by_role in (spec.get("users") or {}).items():
        out[scope] = {}
        for role, grant in by_role.items():
            tools = grant.get("tools")
            out[scope][role] = UserGrant(
                tools=frozenset(tools) if tools is not None else None,
                paths=tuple(grant.get("paths") or ()),
                ref=grant.get("ref") or f"grant:{scope}:{role}",
            )
    return out


@dataclass
class HarnessSystem:
    config: HarnessConfig
    repos: Repositories
    artifacts: FileArtifactStore
    agents: AgentRegistry
    capabilities: CapabilityRegistry
    policy: WhitelistPolicy
    gateway: ToolGateway
    verifier: CapabilityVerifier
    builder: ResumingContextBuilder
    runner: AgentRunner
    tasks: TaskService
    proactive: ProactiveController
    delivery: DeliveryService
    scheduler: Scheduler
    _data: DataConnector
    _owned_pg: PgRepositories | None = field(default=None, repr=False)

    @asynccontextmanager
    async def lifespan(self):
        """Check and close the PostgreSQL store created by this system.

        Explicitly injected repositories remain the caller's responsibility.
        A configured database failure is reported before serving or claiming
        tasks; it never selects an empty in-memory store instead.
        """
        try:
            if self._owned_pg is not None:
                await self._owned_pg.check_ready()
            yield self
        finally:
            self._data.close()
            if self._owned_pg is not None:
                await self._owned_pg.close()

    def worker(self, *, lanes=(RunLane.FOREGROUND,), worker_id: str | None = None) -> Worker:
        return Worker(
            self.tasks,
            self.builder,
            self.runner,
            self.agents,
            lanes=lanes,
            heartbeat_interval_sec=self.config.heartbeat_interval_sec,
            release_attempt=self._data.release,
            worker_id=worker_id,
            grant_refs=lambda task: [
                g.ref
                for g in [self.policy.user_grant(task.scope_id, task.agent_role)]
                if g is not None and g.ref
            ],
        )


def build_system(
    config: HarnessConfig,
    model: ModelAdapter,
    *,
    repos: Repositories | None = None,
    policy_spec: Mapping[str, Any] | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> HarnessSystem:
    """Wire modules over an injected store, configured PostgreSQL, or memory.

    Application entry points must enter the returned system's ``lifespan``
    to initialize and close any database pool this function creates.
    """
    agents = AgentRegistry.builtin()
    capabilities = CapabilityRegistry.builtin()
    spec = policy_spec if policy_spec is not None else load_policy_file(config.policy_file)
    policy = WhitelistPolicy(
        roles=main_role_whitelist(),
        users=user_grants(spec),
        capabilities=capabilities.tool_map(),
        disabled_tools=spec.get("disabled_tools") or (),
    )
    owned_pg = None
    if repos is None:
        if config.database_dsn:
            from .storage.postgres import PgRepositories

            owned_pg = PgRepositories.from_dsn(config.database_dsn)
            repos = owned_pg
        else:
            repos = InMemoryRepositories()
    artifacts = FileArtifactStore(config.artifact_root, repos.artifacts)
    delivery = DeliveryService(repos, artifacts, policy, clock=clock)
    connect, *other_data = data_connectors(artifacts)
    profile = DataProfileConnector(connect, artifacts)
    connectors = [
        *p0_connectors(artifacts),
        connect,
        *other_data,
        profile,
        BackgroundResultsConnector(delivery),
    ]
    gateway = ToolGateway(
        repos,
        artifacts,
        policy,
        connectors,
        profiles=agents.tool_policies(),
        clock=clock,
        delivery=delivery,
    )
    data_checks = DataCardChecks(repos, policy, clock, profile)
    verifier = CapabilityVerifier(repos, artifacts, capabilities, clock=clock,
        checkers={**DEFAULT_CHECKERS, "data_card_schema": data_checks.schema, "stats_replay": data_checks.replay})
    builder = SimpleContextBuilder(
        repos,
        instructions=PackageInstructions(),
        tools=PolicyToolCatalog(policy, gateway.specs),
        capabilities=capabilities,
        clock=clock,
        delivery=delivery,
    )
    builder = ResumingContextBuilder(builder, artifacts)
    runner = AgentRunner(repos, model, gateway, verifier, artifacts, reminders=config.budget_reminder, clock=clock)
    tasks = TaskService(
        repos.tasks, config, artifacts=artifacts, capabilities=capabilities, agents=agents, clock=clock, delivery=delivery
    )
    proactive = ProactiveController(repos, tasks, policy, scope_limit=config.background_scope_limit, clock=clock)
    recovery = Recovery(repos.tasks, config, clock, gateway=gateway, actions=repos.actions, events=repos.events)
    return HarnessSystem(
        config=config,
        repos=repos,
        artifacts=artifacts,
        agents=agents,
        capabilities=capabilities,
        policy=policy,
        gateway=gateway,
        verifier=verifier,
        builder=builder,
        runner=runner,
        tasks=tasks,
        proactive=proactive,
        delivery=delivery,
        scheduler=Scheduler(repos, proactive, recovery, clock=clock),
        _data=connect,
        _owned_pg=owned_pg,
    )
