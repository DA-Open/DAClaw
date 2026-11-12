"""Harness configuration.

Values come from environment variables prefixed ``DARAOS_HARNESS_``. Secrets
(the database DSN) are read from the environment only and are never printed:
``HarnessConfig.__repr__`` and ``safe_dict()`` mask them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path

from .contracts import Limits, LimitsExceeded

ENV_PREFIX = "DARAOS_HARNESS_"


@dataclass(frozen=True)
class DeploymentCaps:
    """Upper bounds the server enforces on any task's limits (spec 5.1)."""

    timeout_sec: int = 4 * 3600
    max_model_calls: int = 200
    max_tool_calls: int = 500
    max_total_tokens: int = 2_000_000

    def check(self, limits: Limits) -> None:
        over = [
            f.name
            for f in fields(self)
            if getattr(limits, f.name) > getattr(self, f.name)
        ]
        if over:
            raise LimitsExceeded(
                f"limits above the deployment caps: {', '.join(over)}",
                fields=over,
            )


@dataclass(frozen=True)
class BudgetReminderConfig:
    """Reminders the Runner adds before a model call (spec comment 6).

    ``thresholds`` are remaining-budget fractions, from loosest to tightest.
    ``final_turn`` adds one more reminder when only the wrap-up call is left.
    ``reserve_model_calls`` model calls are kept for the wrap-up turn.
    """

    enabled: bool = True
    thresholds: tuple[float, ...] = (0.5, 0.2)
    final_turn: bool = True
    reserve_model_calls: int = 1

    def __post_init__(self) -> None:
        if any(not 0.0 < t < 1.0 for t in self.thresholds):
            raise ValueError("budget reminder thresholds must be between 0 and 1")
        if list(self.thresholds) != sorted(self.thresholds, reverse=True):
            raise ValueError("budget reminder thresholds must go from loosest to tightest")
        if len(set(self.thresholds)) != len(self.thresholds):
            raise ValueError("budget reminder thresholds must be distinct")
        if self.reserve_model_calls < 0:
            raise ValueError("reserve_model_calls must be >= 0")


@dataclass(frozen=True)
class HarnessConfig:
    database_dsn: str | None = field(default=None, repr=False)
    artifact_root: Path = Path("./harness_artifacts")
    lease_ttl_sec: int = 60
    heartbeat_interval_sec: int = 15
    default_limits: Limits = field(default_factory=Limits)
    caps: DeploymentCaps = field(default_factory=DeploymentCaps)
    budget_reminder: BudgetReminderConfig = field(default_factory=BudgetReminderConfig)

    def __post_init__(self) -> None:
        if self.lease_ttl_sec <= 0 or self.heartbeat_interval_sec <= 0:
            raise ValueError("lease_ttl_sec and heartbeat_interval_sec must be positive")
        if self.heartbeat_interval_sec >= self.lease_ttl_sec:
            raise ValueError("heartbeat_interval_sec must be shorter than lease_ttl_sec")
        self.caps.check(self.default_limits)

    def __repr__(self) -> str:
        return f"HarnessConfig({self.safe_dict()!r})"

    def safe_dict(self) -> dict[str, object]:
        """Config for logs: the DSN is reduced to whether it is set."""
        return {
            "database_dsn": "<set>" if self.database_dsn else None,
            "artifact_root": str(self.artifact_root),
            "lease_ttl_sec": self.lease_ttl_sec,
            "heartbeat_interval_sec": self.heartbeat_interval_sec,
            "default_limits": self.default_limits.model_dump(),
            "caps": {f.name: getattr(self.caps, f.name) for f in fields(self.caps)},
            "budget_reminder": {
                "enabled": self.budget_reminder.enabled,
                "thresholds": list(self.budget_reminder.thresholds),
                "final_turn": self.budget_reminder.final_turn,
                "reserve_model_calls": self.budget_reminder.reserve_model_calls,
            },
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HarnessConfig:
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = env.get(ENV_PREFIX + name)
            return value.strip() if value and value.strip() else None

        def get_int(name: str, default: int) -> int:
            raw = get(name)
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"{ENV_PREFIX}{name} must be an integer") from None

        def get_bool(name: str, default: bool) -> bool:
            raw = get(name)
            if raw is None:
                return default
            lowered = raw.lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{ENV_PREFIX}{name} must be true or false")

        base_limits = Limits()
        base_caps = DeploymentCaps()
        base_reminder = BudgetReminderConfig()

        thresholds_raw = get("BUDGET_REMINDER_THRESHOLDS")
        if thresholds_raw is None:
            thresholds = base_reminder.thresholds
        else:
            try:
                thresholds = tuple(float(p) for p in thresholds_raw.split(",") if p.strip())
            except ValueError:
                raise ValueError(
                    f"{ENV_PREFIX}BUDGET_REMINDER_THRESHOLDS must be comma-separated numbers"
                ) from None

        return cls(
            database_dsn=get("DATABASE_DSN"),
            artifact_root=Path(get("ARTIFACT_ROOT") or "./harness_artifacts"),
            lease_ttl_sec=get_int("LEASE_TTL_SEC", 60),
            heartbeat_interval_sec=get_int("HEARTBEAT_INTERVAL_SEC", 15),
            default_limits=Limits(
                timeout_sec=get_int("DEFAULT_TIMEOUT_SEC", base_limits.timeout_sec),
                max_model_calls=get_int("DEFAULT_MAX_MODEL_CALLS", base_limits.max_model_calls),
                max_tool_calls=get_int("DEFAULT_MAX_TOOL_CALLS", base_limits.max_tool_calls),
                max_total_tokens=get_int("DEFAULT_MAX_TOTAL_TOKENS", base_limits.max_total_tokens),
            ),
            caps=DeploymentCaps(
                timeout_sec=get_int("CAP_TIMEOUT_SEC", base_caps.timeout_sec),
                max_model_calls=get_int("CAP_MAX_MODEL_CALLS", base_caps.max_model_calls),
                max_tool_calls=get_int("CAP_MAX_TOOL_CALLS", base_caps.max_tool_calls),
                max_total_tokens=get_int("CAP_MAX_TOTAL_TOKENS", base_caps.max_total_tokens),
            ),
            budget_reminder=BudgetReminderConfig(
                enabled=get_bool("BUDGET_REMINDER_ENABLED", base_reminder.enabled),
                thresholds=thresholds,
                final_turn=get_bool("BUDGET_REMINDER_FINAL_TURN", base_reminder.final_turn),
                reserve_model_calls=get_int(
                    "BUDGET_REMINDER_RESERVE_MODEL_CALLS", base_reminder.reserve_model_calls
                ),
            ),
        )
