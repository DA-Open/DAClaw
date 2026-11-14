"""Model budget and budget reminders (runtime/budget.py)."""

import pytest

from harness.config import BudgetReminderConfig
from harness.contracts import Limits, RemainingBudget, ReservationResult, Usage
from harness.runtime.budget import (
    REMINDER_PREFIX,
    BudgetReminders,
    ModelBudget,
    ModelReservation,
    ReminderState,
)
from harness.storage.memory import InMemoryRepositories

from ..fakes import T0, claimed_task

pytestmark = pytest.mark.asyncio

LIMITS = Limits(max_model_calls=20, max_tool_calls=50, max_total_tokens=100_000, timeout_sec=1800)


def remaining(model_calls=20, tool_calls=50, tokens=100_000, seconds=1800.0, limits=LIMITS):
    used = Usage(
        model_calls=limits.max_model_calls - model_calls,
        tool_calls=limits.max_tool_calls - tool_calls,
        input_tokens=limits.max_total_tokens - tokens,
    )
    return RemainingBudget.compute(limits, used, limits.timeout_sec - seconds)


# -- reminders ---------------------------------------------------------------------


async def test_no_reminder_with_plenty_left():
    assert BudgetReminders(BudgetReminderConfig()).check(remaining(), LIMITS, ReminderState()) is None


async def test_tiers_fire_once_each_in_order():
    reminders = BudgetReminders(BudgetReminderConfig())
    state = ReminderState()
    seen = []
    for left in range(20, 0, -1):
        r = reminders.check(remaining(model_calls=left), LIMITS, state)
        if r is not None:
            seen.append((left, r.tier))
    assert seen == [(10, "50%"), (4, "20%"), (2, "last_tool_turn"), (1, "wrap_up")]


async def test_jumping_two_tiers_sends_only_the_tightest():
    reminders = BudgetReminders(BudgetReminderConfig())
    state = ReminderState()
    r = reminders.check(remaining(tokens=15_000), LIMITS, state)
    assert r is not None and r.tier == "20%"
    # The looser 50% tier is never sent afterwards.
    assert reminders.check(remaining(tokens=14_000), LIMITS, state) is None


async def test_tightest_of_four_limits_decides():
    reminders = BudgetReminders(BudgetReminderConfig())
    r = reminders.check(remaining(seconds=300.0), LIMITS, ReminderState())
    assert r is not None and r.tier == "20%"
    assert "time 5 min" in r.text


async def test_text_states_facts_only():
    r = BudgetReminders(BudgetReminderConfig()).check(remaining(model_calls=4, tokens=18_000), LIMITS, ReminderState())
    assert r.text.startswith(REMINDER_PREFIX)
    assert "model calls 4/20" in r.text and "tokens 18%" in r.text
    for word in ("hurry", "quickly", "soon", "尽快"):
        assert word not in r.text.lower()


async def test_disabled_keeps_wrap_up_turn():
    reminders = BudgetReminders(BudgetReminderConfig(enabled=False, final_turn=False))
    state = ReminderState()
    assert reminders.check(remaining(model_calls=3), LIMITS, state) is None
    assert reminders.check(remaining(model_calls=2), LIMITS, state) is None
    r = reminders.check(remaining(model_calls=1), LIMITS, state)
    assert r is not None and r.tier == "wrap_up"


async def test_no_reserve_means_no_wrap_up_turn():
    reminders = BudgetReminders(BudgetReminderConfig(reserve_model_calls=0))
    assert not reminders.is_wrap_up(remaining(model_calls=1))
    r = reminders.check(remaining(model_calls=1), LIMITS, ReminderState())
    assert r is not None and r.tier == "20%"


async def test_no_tool_calls_left_is_wrap_up():
    reminders = BudgetReminders(BudgetReminderConfig())
    assert reminders.is_wrap_up(remaining(tool_calls=0))


# -- model budget --------------------------------------------------------------------


async def test_reserve_then_settle_charges_real_usage():
    repos = InMemoryRepositories()
    task, ex = await claimed_task(repos)
    budget = ModelBudget(repos.usage)
    r = await budget.reserve(task, ex.lease.attempt_id, "c1", input_tokens=500, max_output_tokens=1000, now=T0)
    assert isinstance(r, ModelReservation)
    await budget.settle(r.call_id, Usage(model_calls=1, input_tokens=420, output_tokens=80), T0)
    usage = (await repos.tasks.get("s1", task.id)).usage
    assert usage == Usage(model_calls=1, input_tokens=420, output_tokens=80)


async def test_settling_twice_charges_once():
    repos = InMemoryRepositories()
    task, ex = await claimed_task(repos)
    budget = ModelBudget(repos.usage)
    r = await budget.reserve(task, ex.lease.attempt_id, "c1", input_tokens=10, max_output_tokens=10, now=T0)
    actual = Usage(model_calls=1, input_tokens=7, output_tokens=3)
    await budget.settle(r.call_id, actual, T0)
    await budget.settle(r.call_id, actual, T0)
    assert (await repos.tasks.get("s1", task.id)).usage == actual


async def test_settle_always_counts_one_model_call():
    repos = InMemoryRepositories()
    task, ex = await claimed_task(repos)
    budget = ModelBudget(repos.usage)
    r = await budget.reserve(task, ex.lease.attempt_id, "c1", input_tokens=10, max_output_tokens=10, now=T0)
    await budget.settle(r.call_id, Usage(input_tokens=5), T0)  # adapter forgot model_calls
    assert (await repos.tasks.get("s1", task.id)).usage.model_calls == 1


async def test_reserve_refused_when_calls_used_up():
    repos = InMemoryRepositories()
    task, ex = await claimed_task(repos, limits=Limits(max_model_calls=1))
    budget = ModelBudget(repos.usage)
    first = await budget.reserve(task, ex.lease.attempt_id, "c1", input_tokens=10, max_output_tokens=10, now=T0)
    assert isinstance(first, ModelReservation)
    second = await budget.reserve(task, ex.lease.attempt_id, "c2", input_tokens=10, max_output_tokens=10, now=T0)
    assert isinstance(second, ReservationResult) and second.exceeded == ["max_model_calls"]


async def test_output_cap_shrinks_to_fit_token_budget():
    repos = InMemoryRepositories()
    task, ex = await claimed_task(repos, limits=Limits(max_total_tokens=3000))
    r = await ModelBudget(repos.usage).reserve(
        task, ex.lease.attempt_id, "c1", input_tokens=1000, max_output_tokens=4096, now=T0
    )
    assert isinstance(r, ModelReservation)
    assert r.max_output_tokens == 2000


async def test_too_few_tokens_left_is_refused():
    repos = InMemoryRepositories()
    task, ex = await claimed_task(repos, limits=Limits(max_total_tokens=1100))
    r = await ModelBudget(repos.usage).reserve(
        task, ex.lease.attempt_id, "c1", input_tokens=1000, max_output_tokens=4096, now=T0
    )
    assert isinstance(r, ReservationResult) and not r.granted
