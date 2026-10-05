from decimal import Decimal as D

import pytest

from app.domain.compensation import (
    Component,
    TotalChange,
    annualize,
    average_increase,
    counts_as_increase,
    percentage_change,
    total_compensation,
)


@pytest.mark.parametrize(
    ("amount", "period_months", "annual"),
    [
        (D(10000), 1, D(120000)),  # monthly
        (D(15000), 3, D(60000)),  # quarterly
        (D(30000), 6, D(60000)),  # semi-annual
        (D(50000), 12, D(50000)),  # annual
        (D(24000), 24, D(12000)),  # every two years
    ],
)
def test_annualize(amount, period_months, annual):
    assert annualize(amount, period_months) == annual


@pytest.mark.parametrize("period_months", [0, -1])
def test_annualize_rejects_non_positive_period(period_months):
    with pytest.raises(ValueError):
        annualize(D(1), period_months)


def test_total_sums_annualized_components_that_count():
    components = [
        Component(D(10000), 1),  # base: 120k
        Component(D(5000), 3),  # quarterly bonus: 20k
        Component(D(1000), 1, counts_toward_total=True),  # internet in CTC: 12k
        Component(D(2000), 12, counts_toward_total=False),  # wellness outside CTC
    ]
    assert total_compensation(components) == D(152000)


def test_total_of_nothing_is_zero():
    assert total_compensation([]) == D(0)


def test_percentage_change():
    assert percentage_change(D(100), D(110)) == D(10)
    assert percentage_change(D(200), D(150)) == D(-25)
    assert percentage_change(D(0), D(100)) is None


def change(prev, new, reason="annual_revision", prev_cur="INR", new_cur="INR"):
    return TotalChange(D(prev), D(new), prev_cur, new_cur, reason)


def test_corrections_and_currency_changes_are_not_increases():
    assert counts_as_increase(change(100, 110))
    assert not counts_as_increase(change(100, 90, reason="correction"))
    assert not counts_as_increase(change(100, 2, prev_cur="INR", new_cur="USD"))
    assert not counts_as_increase(change(0, 100, reason="new_hire"))


def test_average_increase_excludes_corrections_and_currency_changes():
    changes = [
        change(100, 110),  # +10%
        change(200, 240),  # +20%
        change(100, 50, reason="correction"),  # ignored
        change(100_000, 1_200, prev_cur="INR", new_cur="USD"),  # ignored
    ]
    assert average_increase(changes) == D(15)


def test_average_increase_of_nothing_eligible_is_none():
    assert average_increase([change(100, 90, reason="correction")]) is None
