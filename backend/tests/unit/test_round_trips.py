"""Fewer database round trips per request (Scale Lab E24): the remembered rates and currencies,
the once-a-day promotion check, and the ping only after a connection has been idle."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.cache import ExpiringValue
from app.services import compensation, exchange_rates


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


# --- ExpiringValue ---


def test_a_value_is_computed_once_within_its_time_and_again_after():
    clock, calls = Clock(), []
    value = ExpiringValue[int](ttl=60, clock=clock)

    def compute() -> int:
        calls.append(1)
        return len(calls)

    assert (value.get(compute), value.get(compute)) == (1, 1)
    clock.now = 61
    assert value.get(compute) == 2


def test_invalidate_forgets_the_value_at_once():
    calls = []
    value = ExpiringValue[int](ttl=60)

    def compute() -> int:
        calls.append(1)
        return len(calls)

    value.get(compute)
    value.invalidate()
    assert value.get(compute) == 2


def test_a_value_computed_while_a_write_lands_is_not_kept():
    value = ExpiringValue[str](ttl=60)

    def compute() -> str:
        value.invalidate()  # a write lands while this is being computed
        return "old"

    assert value.get(compute) == "old"
    assert value.get(lambda: "new") == "new"  # the stale value was not stored


def test_a_zero_time_remembers_nothing():
    calls = []
    value = ExpiringValue[int](ttl=0)
    value.get(lambda: calls.append(1))
    value.get(lambda: calls.append(1))
    assert len(calls) == 2


# --- currencies and rates ---


class FakeSession:
    def __init__(self, known):
        self.known, self.lookups = set(known), 0

    def get(self, model, code):
        self.lookups += 1
        return object() if code in self.known else None


@pytest.fixture
def reference_cache_on():
    exchange_rates.rates_cache.ttl = 60
    exchange_rates.rates_cache.invalidate()
    exchange_rates.known_currencies.clear()
    yield
    exchange_rates.rates_cache.ttl = 0
    exchange_rates.rates_cache.invalidate()
    exchange_rates.known_currencies.clear()


def test_a_currency_seen_once_is_not_looked_up_again(reference_cache_on):
    session = FakeSession({"EUR"})

    assert exchange_rates.require_supported_currency(session, "eur") == "EUR"
    assert exchange_rates.require_supported_currency(session, "EUR") == "EUR"
    assert session.lookups == 1


def test_an_unknown_currency_is_always_checked_and_refused(reference_cache_on):
    session = FakeSession(set())
    for _ in range(2):
        with pytest.raises(Exception, match="XXX"):
            exchange_rates.require_supported_currency(session, "xxx")
    assert session.lookups == 2


def test_a_currency_added_after_a_refusal_works_at_once(reference_cache_on):
    session = FakeSession(set())
    with pytest.raises(Exception, match="NEW"):
        exchange_rates.require_supported_currency(session, "NEW")
    session.known.add("NEW")
    assert exchange_rates.require_supported_currency(session, "NEW") == "NEW"


def test_the_latest_rates_are_remembered_until_a_write(reference_cache_on, monkeypatch):
    loads = []

    def load(session):
        loads.append(1)
        return ({"EUR": Decimal("1.1")}, {"EUR": date(2026, 1, 1)})

    monkeypatch.setattr(exchange_rates, "load_latest_rates", load)

    exchange_rates.latest_rates(None)
    exchange_rates.latest_rates(None)
    assert len(loads) == 1
    exchange_rates.forget_rates()  # what the API does after any write
    exchange_rates.latest_rates(None)
    assert len(loads) == 2


# --- promotion once a day ---


@pytest.fixture
def promotion_once_a_day(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "promote_once_per_day", True)
    monkeypatch.setattr(compensation, "_promoted_on", None)
    return get_settings()


def spy_promotion(monkeypatch, results):
    calls = []

    def fake(session, today=None):
        calls.append(today)
        return results.pop(0)

    monkeypatch.setattr(compensation, "promote_due_records", fake)
    return calls


def test_once_nothing_is_due_the_check_is_skipped_for_the_rest_of_the_day(
    promotion_once_a_day, monkeypatch
):
    calls = spy_promotion(monkeypatch, [0, 0, 0])

    for _ in range(3):
        compensation.promote_due_records_for_request(None)

    assert len(calls) == 1


def test_the_check_runs_again_when_the_date_changes(promotion_once_a_day, monkeypatch):
    calls = spy_promotion(monkeypatch, [0, 0])
    monkeypatch.setattr(compensation, "utc_today", lambda: date(2026, 5, 1))
    compensation.promote_due_records_for_request(None)
    monkeypatch.setattr(
        compensation, "utc_today", lambda: date(2026, 5, 1) + timedelta(days=1)
    )

    compensation.promote_due_records_for_request(None)

    assert len(calls) == 2


def test_a_promotion_that_moved_rows_is_not_trusted_until_it_finds_nothing(
    promotion_once_a_day, monkeypatch
):
    calls = spy_promotion(monkeypatch, [3, 0, 0])

    for _ in range(3):
        compensation.promote_due_records_for_request(None)

    assert len(calls) == 2  # moved rows (may be rolled back), then empty, then skipped


def test_the_check_runs_every_time_when_the_setting_is_off(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "promote_once_per_day", False)
    monkeypatch.setattr(compensation, "_promoted_on", None)
    calls = spy_promotion(monkeypatch, [0, 0, 0])

    for _ in range(3):
        compensation.promote_due_records_for_request(None)

    assert len(calls) == 3
