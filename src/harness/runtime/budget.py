"""Model-call budget and budget reminders (spec 6.2, review comment 6).

The Runner owns model usage: it reserves before each call and settles after,
once per ``call_id``. Tool usage is reserved by ToolGateway together with
the action row and is not handled here.

Reminders state the remaining budget before a model call. They never tell
the model to hurry: upstream hermes removed its mid-run warnings because they
made models give up early (``run_agent.py``, upstream issue #7915).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..config import BudgetReminderConfig
from ..contracts import (
    Limits,
    RemainingBudget,
    ReservationResult,
    Task,
    Usage,
    UsageKind,
    UsageReservation,
)
from ..storage.repositories import UsageRepository

REMINDER_PREFIX = "[budget]"

# Output tokens a call needs at least to be worth making when the token
# budget is nearly used up.
MIN_OUTPUT_TOKENS = 256


@dataclass(frozen=True)
class ModelReservation:
    """A granted model-call reservation and the output cap that fits it."""

    call_id: str
    max_output_tokens: int
    reserved: Usage


class ModelBudget:
    """Reserve and settle model calls through ``UsageRepository``."""

    def __init__(self, usage: UsageRepository) -> None:
        self._usage = usage

    async def reserve(
        self,
        task: Task,
        attempt_id: str,
        call_id: str,
        *,
        input_tokens: int,
        max_output_tokens: int,
        now: datetime,
    ) -> ModelReservation | ReservationResult:
        """Reserve one call: estimated input plus the full output cap.

        If only the token limit is in the way, retry once with the output cap
        cut to what is left, as long as ``MIN_OUTPUT_TOKENS`` still fit.
        Returns the refused ``ReservationResult`` otherwise.
        """
        amount = Usage(model_calls=1, input_tokens=input_tokens, output_tokens=max_output_tokens)
        result = await self._usage.reserve(task.id, attempt_id, call_id, UsageKind.MODEL, amount, now)
        if result.granted:
            return ModelReservation(call_id, max_output_tokens, amount)
        if result.exceeded != ["max_total_tokens"]:
            return result
        open_usage = sum(
            (r.reserved for r in await self._usage.open_reservations(task.id)), Usage()
        )
        left = task.limits.max_total_tokens - task.usage.total_tokens - open_usage.total_tokens - input_tokens
        if left < MIN_OUTPUT_TOKENS:
            return result
        smaller = amount.model_copy(update={"output_tokens": left})
        retry_id = f"{call_id}.fit"
        result = await self._usage.reserve(task.id, attempt_id, retry_id, UsageKind.MODEL, smaller, now)
        if result.granted:
            return ModelReservation(retry_id, left, smaller)
        return result

    async def settle(self, call_id: str, actual: Usage, now: datetime) -> UsageReservation:
        """Charge real usage. A call always counts as one model call."""
        actual = actual.model_copy(update={"model_calls": 1, "tool_calls": 0})
        return await self._usage.settle(call_id, actual, now)

    async def release(self, call_id: str, now: datetime) -> UsageReservation:
        """Only for calls the provider surely did not run (it answered with an
        error status). Timeouts and lost connections are settled at the
        reserved amount instead (spec 6.2 step 4)."""
        return await self._usage.release(call_id, now)


# -- reminders -----------------------------------------------------------------


@dataclass(frozen=True)
class Reminder:
    tier: str
    text: str


@dataclass
class ReminderState:
    """Which tiers were already sent in this task (kept across rounds)."""

    sent: set[str] = field(default_factory=set)


class BudgetReminders:
    """Pick the reminder, if any, to add before the next model call.

    Tiers, loosest first: one per ``thresholds`` entry (``"50%"``, ``"20%"``),
    then ``"last_tool_turn"`` when the next call is the last one that may use
    tools, then ``"wrap_up"`` on the call kept for the result. Each tier is
    sent once; when several are crossed at once only the tightest is sent
    and the looser ones count as sent.
    """

    def __init__(self, config: BudgetReminderConfig) -> None:
        self.config = config

    def tier_names(self) -> list[str]:
        return [f"{round(t * 100)}%" for t in self.config.thresholds] + ["last_tool_turn", "wrap_up"]

    def is_wrap_up(self, remaining: RemainingBudget) -> bool:
        """True when the next call gets no tools and must return the result.

        That is when only the reserved wrap-up calls are left, or no tool
        call is left at all.
        """
        reserve = self.config.reserve_model_calls
        if reserve > 0 and remaining.model_calls <= reserve:
            return True
        return remaining.tool_calls <= 0

    def check(self, remaining: RemainingBudget, limits: Limits, state: ReminderState) -> Reminder | None:
        """Switches: ``enabled`` turns the percentage tiers on, ``final_turn``
        the last-tool-turn notice. The wrap-up notice is part of the wrap-up
        turn itself (it is how the model learns tools are gone), so it is
        sent whenever that turn happens; ``reserve_model_calls=0`` turns the
        wrap-up turn off."""
        crossed: list[str] = []
        if self.config.enabled:
            crossed += [f"{round(t * 100)}%" for t in self.config.thresholds if remaining.fraction <= t]
        if self.is_wrap_up(remaining):
            crossed.append("wrap_up")
        elif self.config.final_turn and self._is_last_tool_turn(remaining):
            crossed.append("last_tool_turn")
        new = [t for t in crossed if t not in state.sent]
        if not new:
            return None
        order = self.tier_names()
        tightest = max(new, key=order.index)
        # Every tier up to the tightest counts as sent, so a looser one is
        # never sent after a tighter one.
        state.sent.update(order[: order.index(tightest) + 1])
        return Reminder(tightest, self.text(tightest, remaining, limits))

    def _is_last_tool_turn(self, remaining: RemainingBudget) -> bool:
        reserve = self.config.reserve_model_calls
        return reserve > 0 and remaining.model_calls == reserve + 1

    @staticmethod
    def text(tier: str, remaining: RemainingBudget, limits: Limits) -> str:
        facts = describe(remaining, limits)
        if tier == "wrap_up":
            return (
                f"{REMINDER_PREFIX} {facts}. Tools are not available for this turn. "
                "Return the result now with submit_result, naming the artifacts already written."
            )
        if tier == "last_tool_turn":
            return (
                f"{REMINDER_PREFIX} {facts}. This is the last turn with tools; "
                "the next turn has no tools and must return the result."
            )
        return f"{REMINDER_PREFIX} {facts}."


def describe(remaining: RemainingBudget, limits: Limits) -> str:
    """"Remaining: model calls 4/20, tool calls 30/50, tokens 18% ..."."""
    tokens_pct = round(100 * remaining.total_tokens / limits.max_total_tokens)
    minutes = remaining.seconds / 60
    return (
        f"Remaining: model calls {remaining.model_calls}/{limits.max_model_calls}, "
        f"tool calls {remaining.tool_calls}/{limits.max_tool_calls}, "
        f"tokens {tokens_pct}% ({remaining.total_tokens}/{limits.max_total_tokens}), "
        f"time {minutes:.0f} min"
    )
