"""Exchange-rate storage, refresh and the converter API, against Postgres.

The provider is always faked with httpx.MockTransport — tests never call the live API.
"""

from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.db.session import get_db
from app.main import app
from app.models import ExchangeRate
from app.seed.reference import load_reference_data
from app.services.exchange_rates import latest_rates, refresh_exchange_rates

# Far-future dates so these fake rates are always "latest", even if the dev DB has real ones.
DAY_1 = 4070908800  # 2099-01-01 00:00 UTC
DAY_2 = 4070995200  # 2099-01-02 00:00 UTC


class FakeProvider(httpx.Client):
    def __init__(self, payload: dict | None, status_code: int) -> None:
        self.calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return httpx.Response(status_code, json=payload or {})

        super().__init__(transport=httpx.MockTransport(handler))


def provider(payload: dict | None = None, status_code: int = 200) -> FakeProvider:
    return FakeProvider(payload, status_code)


def payload(rates: dict, when: int = DAY_1) -> dict:
    return {
        "result": "success",
        "base_code": "USD",
        "time_last_update_unix": when,
        "rates": rates,
    }


@pytest.fixture
def seeded(db):
    load_reference_data(db)
    return db


def test_refresh_stores_supported_currencies_only(seeded):
    result = refresh_exchange_rates(
        seeded,
        provider(payload({"USD": 1, "INR": 80, "NOT_A_CURRENCY": 5})),
        force=True,
    )

    assert result.ok
    assert result.rate_date == date(2099, 1, 1)
    assert result.stored == 2
    assert "EUR" in result.missing  # supported, but this fake provider didn't quote it
    rates, dates = latest_rates(seeded)
    assert rates["INR"] == Decimal("0.0125")
    assert dates["INR"] == date(2099, 1, 1)
    assert "NOT_A_CURRENCY" not in rates


def test_refetching_same_day_updates_instead_of_duplicating(seeded):
    refresh_exchange_rates(seeded, provider(payload({"USD": 1, "INR": 80})), force=True)
    refresh_exchange_rates(
        seeded, provider(payload({"USD": 1, "INR": 100})), force=True
    )

    count = seeded.scalar(
        select(func.count())
        .select_from(ExchangeRate)
        .where(
            ExchangeRate.currency == "INR", ExchangeRate.rate_date == date(2099, 1, 1)
        )
    )
    assert count == 1
    assert latest_rates(seeded)[0]["INR"] == Decimal("0.01")


def test_latest_rates_prefers_most_recent_date(seeded):
    refresh_exchange_rates(
        seeded, provider(payload({"USD": 1, "INR": 80}, DAY_1)), force=True
    )
    refresh_exchange_rates(
        seeded, provider(payload({"USD": 1, "INR": 100}, DAY_2)), force=True
    )

    rates, dates = latest_rates(seeded)
    assert rates["INR"] == Decimal("0.01")
    assert dates["INR"] == date(2099, 1, 2)


@pytest.mark.parametrize(
    "client",
    [
        provider(status_code=503),
        provider({"result": "error", "error-type": "quota-reached"}),
    ],
    ids=["http-error", "provider-error"],
)
def test_failed_refresh_keeps_stored_rates(seeded, client, caplog):
    refresh_exchange_rates(seeded, provider(payload({"USD": 1, "INR": 80})), force=True)

    result = refresh_exchange_rates(seeded, client, force=True)

    assert not result.ok
    assert "keeping latest stored rates" in caplog.text
    assert latest_rates(seeded)[0]["INR"] == Decimal("0.0125")


def test_refresh_skips_provider_while_rates_are_fresh(seeded):
    refresh_exchange_rates(seeded, provider(payload({"USD": 1, "INR": 80})), force=True)
    client = provider(payload({"USD": 1, "INR": 100}))

    result = refresh_exchange_rates(seeded, client)

    assert result.ok and result.skipped
    assert result.rate_date == date(2099, 1, 1)
    assert client.calls == 0
    assert latest_rates(seeded)[0]["INR"] == Decimal("0.0125")


def test_refresh_fetches_once_rates_are_stale(seeded):
    refresh_exchange_rates(seeded, provider(payload({"USD": 1, "INR": 80})), force=True)
    seeded.execute(
        text("UPDATE exchange_rates SET fetched_at = now() - interval '25 hours'")
    )
    client = provider(payload({"USD": 1, "INR": 100}))

    result = refresh_exchange_rates(seeded, client)

    assert result.ok and not result.skipped
    assert client.calls == 1
    assert latest_rates(seeded)[0]["INR"] == Decimal("0.01")


def test_force_fetches_even_when_fresh(seeded):
    refresh_exchange_rates(seeded, provider(payload({"USD": 1, "INR": 80})), force=True)
    client = provider(payload({"USD": 1, "INR": 100}))

    result = refresh_exchange_rates(seeded, client, force=True)

    assert not result.skipped
    assert client.calls == 1


# --- converter API ---


@pytest.fixture
def api(seeded):
    refresh_exchange_rates(
        seeded, provider(payload({"USD": 1, "INR": 80, "EUR": 0.8})), force=True
    )
    app.dependency_overrides[get_db] = lambda: seeded
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_convert_endpoint(api):
    response = api.get(
        "/exchange-rates/convert", params={"amount": "2.50", "from": "eur", "to": "INR"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["from_currency"] == "EUR"
    assert Decimal(body["converted"]) == Decimal("250.00")
    assert Decimal(body["rate"]) == Decimal(100)
    assert body["rates_as_of"] == {"EUR": "2099-01-01", "INR": "2099-01-01"}


def test_convert_rejects_unsupported_currency(api):
    response = api.get(
        "/exchange-rates/convert", params={"amount": "1", "from": "USD", "to": "ABC"}
    )
    assert response.status_code == 422
    assert "ABC" in response.json()["detail"]


def test_convert_reports_supported_currency_without_rate(api):
    response = api.get(
        "/exchange-rates/convert", params={"amount": "1", "from": "USD", "to": "KPW"}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "no exchange rate for KPW"


def test_convert_rejects_negative_amount(api):
    response = api.get(
        "/exchange-rates/convert", params={"amount": "-1", "from": "USD", "to": "INR"}
    )
    assert response.status_code == 422


def test_latest_endpoint_lists_stored_rates(api):
    response = api.get("/exchange-rates/latest")

    assert response.status_code == 200
    by_code = {r["currency"]: r for r in response.json()}
    assert Decimal(by_code["INR"]["rate_to_usd"]) == Decimal("0.0125")


def test_refresh_endpoint_does_not_call_provider_when_fresh(api, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("provider must not be called while rates are fresh")

    monkeypatch.setattr("app.services.exchange_rates.fetch_live_rates", fail)

    response = api.post("/exchange-rates/refresh")

    assert response.status_code == 200
    assert response.json()["skipped"] is True
