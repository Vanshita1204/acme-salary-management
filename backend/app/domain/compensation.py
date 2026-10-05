"""Compensation math. Pure functions: no database or network access."""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

MONTHS_PER_YEAR = 12
HUNDRED = Decimal(100)

# Reasons excluded from average-increase calculations (§5): a correction fixes a
# mistake rather than representing a raise.
NON_INCREASE_REASONS = frozenset({"correction"})


@dataclass(frozen=True)
class Component:
    """One current compensation component of an employee."""

    amount: Decimal  # per period_months, in the employee's currency
    period_months: int
    counts_toward_total: bool = True
    category: str = ""


@dataclass(frozen=True)
class TotalChange:
    """An employee's total compensation before and after one dated change."""

    previous_total: Decimal
    new_total: Decimal
    previous_currency: str
    new_currency: str
    reason_code: str


def annualize(amount: Decimal, period_months: int) -> Decimal:
    """Amount per year for an amount paid every `period_months` months."""
    if period_months <= 0:
        raise ValueError("period_months must be positive")
    return amount * MONTHS_PER_YEAR / period_months


def total_compensation(components: Iterable[Component]) -> Decimal:
    """Annual total compensation (CTC): annualized components that count toward total."""
    return sum(
        (
            annualize(c.amount, c.period_months)
            for c in components
            if c.counts_toward_total
        ),
        Decimal(0),
    )


def percentage_change(previous: Decimal, new: Decimal) -> Decimal | None:
    """Percent change from previous to new; None when there's no base to compare."""
    if previous == 0:
        return None
    return (new - previous) / previous * HUNDRED


def counts_as_increase(change: TotalChange) -> bool:
    """Whether a change belongs in average-increase stats (§5, FR-7).

    Corrections are excluded, and so are currency changes: comparing totals across two
    currencies isn't meaningful (cross-currency increases are explicitly deferred).
    """
    return (
        change.reason_code not in NON_INCREASE_REASONS
        and change.previous_currency == change.new_currency
        and change.previous_total != 0
    )


def average_increase(changes: Iterable[TotalChange]) -> Decimal | None:
    """Mean percentage increase over eligible changes; None when none are eligible."""
    percents = [
        percentage_change(c.previous_total, c.new_total)
        for c in changes
        if counts_as_increase(c)
    ]
    if not percents:
        return None
    return sum(percents, Decimal(0)) / len(percents)
