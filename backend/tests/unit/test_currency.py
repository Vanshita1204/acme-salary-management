from datetime import date
from decimal import Decimal

import pytest

from app.domain.currency import (
    MissingRateError,
    convert,
    cross_rate,
    rates_to_usd_from_usd_base,
)
from app.services.exchange_rates import (
    ExchangeRateProviderError,
    parse_provider_response,
)

# Fixed rates (USD value of one unit) — tests never use live rates.
RATES = {
    "USD": Decimal(1),
    "EUR": Decimal("1.25"),
    "INR": Decimal("0.0125"),  # 80 INR per USD
}


def test_same_currency_is_identity_even_without_a_rate():
    assert convert(Decimal("100.005"), "KPW", "KPW", RATES) == Decimal("100.00")


def test_converts_to_and_from_usd():
    assert convert(Decimal(8000), "INR", "USD", RATES) == Decimal("100.00")
    assert convert(Decimal(100), "USD", "INR", RATES) == Decimal("8000.00")


def test_cross_currency_goes_through_usd():
    # 1 EUR = 1.25 USD = 100 INR
    assert cross_rate("EUR", "INR", RATES) == Decimal(100)
    assert convert(Decimal("2.50"), "EUR", "INR", RATES) == Decimal("250.00")


def test_rounds_half_even_to_cents():
    rates = {"USD": Decimal(1), "AAA": Decimal("0.001")}
    assert convert(Decimal(5), "AAA", "USD", rates) == Decimal("0.00")  # 0.005 -> 0.00
    assert convert(Decimal(15), "AAA", "USD", rates) == Decimal("0.02")  # 0.015 -> 0.02


@pytest.mark.parametrize(("source", "target"), [("XYZ", "USD"), ("USD", "XYZ")])
def test_missing_rate_raises(source, target):
    with pytest.raises(MissingRateError, match="XYZ"):
        convert(Decimal(1), source, target, RATES)


def test_inverts_usd_based_quotes_without_float_noise():
    rates = rates_to_usd_from_usd_base({"USD": 1, "INR": 80, "EUR": 0.8})
    assert rates == {
        "USD": Decimal(1),
        "INR": Decimal("0.0125"),
        "EUR": Decimal("1.25"),
    }


def test_inversion_skips_non_positive_quotes():
    assert rates_to_usd_from_usd_base({"BAD": 0, "USD": 1}) == {"USD": Decimal(1)}


def test_inversion_keeps_precision_for_weak_currencies():
    rate = rates_to_usd_from_usd_base({"IRR": 1480223.722823})["IRR"]
    # Converting back must round-trip to well under a cent per million rial.
    assert abs(Decimal("1480223.722823") * rate - 1) < Decimal("1e-20")


# --- provider response parsing ---

PAYLOAD = {
    "result": "success",
    "base_code": "USD",
    "time_last_update_unix": 1791158551,  # 2026-10-05 00:02:31 UTC
    "rates": {"USD": 1, "INR": 80},
}


def test_parses_provider_payload():
    fetched = parse_provider_response(PAYLOAD, source="open.er-api.com")
    assert fetched.rate_date == date(2026, 10, 5)
    assert fetched.rates_to_usd == {"USD": Decimal(1), "INR": Decimal("0.0125")}
    assert fetched.source == "open.er-api.com"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"result": "error"}, "provider returned 'error'"),
        ({"base_code": "EUR"}, "expected USD base"),
        ({"rates": None}, "malformed"),
    ],
)
def test_rejects_bad_provider_payloads(override, message):
    with pytest.raises(ExchangeRateProviderError, match=message):
        parse_provider_response({**PAYLOAD, **override}, source="x")
