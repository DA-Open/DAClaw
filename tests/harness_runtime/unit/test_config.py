import pytest

from harness.config import BudgetReminderConfig, DeploymentCaps, HarnessConfig
from harness.contracts import Limits, LimitsExceeded

P = "DARAOS_HARNESS_"


def test_defaults_without_env():
    config = HarnessConfig.from_env({})
    assert config.database_dsn is None
    assert config.default_limits == Limits()
    assert config.budget_reminder.thresholds == (0.5, 0.2)
    assert config.budget_reminder.reserve_model_calls == 1
    assert config.lease_ttl_sec == 60


def test_reads_env():
    config = HarnessConfig.from_env(
        {
            P + "DATABASE_DSN": "postgresql://u:secret@db/harness",
            P + "DEFAULT_MAX_MODEL_CALLS": "8",
            P + "BUDGET_REMINDER_THRESHOLDS": "0.6, 0.3,0.1",
            P + "BUDGET_REMINDER_ENABLED": "off",
            P + "LEASE_TTL_SEC": "120",
        }
    )
    assert config.database_dsn == "postgresql://u:secret@db/harness"
    assert config.default_limits.max_model_calls == 8
    assert config.budget_reminder.thresholds == (0.6, 0.3, 0.1)
    assert config.budget_reminder.enabled is False
    assert config.lease_ttl_sec == 120


def test_dsn_never_shown():
    config = HarnessConfig.from_env({P + "DATABASE_DSN": "postgresql://u:secret@db/harness"})
    assert "secret" not in repr(config)
    assert "secret" not in str(config.safe_dict())
    assert config.safe_dict()["database_dsn"] == "<set>"


@pytest.mark.parametrize(
    "env",
    [
        {P + "DEFAULT_MAX_MODEL_CALLS": "many"},
        {P + "BUDGET_REMINDER_ENABLED": "maybe"},
        {P + "BUDGET_REMINDER_THRESHOLDS": "0.2,0.5"},
        {P + "BUDGET_REMINDER_THRESHOLDS": "1.5"},
        {P + "BUDGET_REMINDER_THRESHOLDS": "a,b"},
        {P + "HEARTBEAT_INTERVAL_SEC": "60", P + "LEASE_TTL_SEC": "60"},
    ],
)
def test_bad_env_is_rejected(env):
    with pytest.raises(ValueError):
        HarnessConfig.from_env(env)


def test_caps_reject_large_limits():
    caps = DeploymentCaps(max_model_calls=10)
    caps.check(Limits(max_model_calls=10))
    with pytest.raises(LimitsExceeded) as info:
        caps.check(Limits(max_model_calls=11, max_tool_calls=10_000))
    assert info.value.details["fields"] == ["max_model_calls", "max_tool_calls"]


def test_default_limits_must_fit_caps():
    with pytest.raises(LimitsExceeded):
        HarnessConfig(default_limits=Limits(max_model_calls=50), caps=DeploymentCaps(max_model_calls=10))


def test_reminder_thresholds_distinct():
    with pytest.raises(ValueError):
        BudgetReminderConfig(thresholds=(0.5, 0.5))
