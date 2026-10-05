"""Phase 8: the daily job, provider-outage behaviour of reads, reporting currency."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.jobs.daily import run_daily, seconds_until_next_run
from app.models import ExchangeRate
from app.seed.change_reasons import load_change_reasons
from app.seed.compensation_types import load_compensation_types
from app.seed.reference import load_reference_data
from app.services.compensation import utc_today
from app.services.exchange_rates import UNSUPPORTED_CURRENCY

# Far-future dates so these rates are "latest" even if the dev DB has real ones.
STORED_DAY = date(2099, 1, 1)
STORED = {"USD": Decimal(1), "EUR": Decimal("1.25"), "INR": Decimal("0.0125")}
FETCHED_DAY = 4071081600  # 2099-01-03 00:00 UTC
MONTHLY_INR = Decimal(200000)  # 2.4M INR a year = 30k USD = 24k EUR at STORED


class FakeProvider(httpx.Client):
    def __init__(self, status_code: int = 200, rates: dict | None = None) -> None:
        self.calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            body = {
                "result": "success",
                "base_code": "USD",
                "time_last_update_unix": FETCHED_DAY,
                "rates": rates or {"USD": 1, "EUR": 0.5, "INR": 100},
            }
            return httpx.Response(status_code, json=body)

        super().__init__(transport=httpx.MockTransport(handler))


@pytest.fixture
def stored_rates(db):
    """Yesterday's rates, fetched long enough ago that the job tries the provider."""
    load_reference_data(db)
    db.add_all(
        ExchangeRate(currency=c, rate_to_usd=r, rate_date=STORED_DAY, source="pytest")
        for c, r in STORED.items()
    )
    db.flush()
    db.execute(text("UPDATE exchange_rates SET fetched_at = now() - interval '2 days'"))


@pytest.fixture
def employee(client, db, org, stored_rates) -> dict:
    load_compensation_types(db)
    load_change_reasons(db)
    company = client.post("/companies", json={"name": f"pytest {uuid.uuid4()}"})
    response = client.post(
        "/employees",
        json={
            "company_id": company.json()["id"],
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": f"{uuid.uuid4().hex[:10]}@pytest.example",
            **org["role"],
            "current_country": "IN",
            "currency": "INR",
            "hire_date": "2024-01-15",
            "base_pay": {"amount": str(MONTHLY_INR)},
            "changed_by": "hr@acme",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def profile(client, employee, currency="EUR") -> dict:
    return client.get(
        f"/employees/{employee['id']}", params={"reporting_currency": currency}
    ).json()


def directory_item(client, employee, currency="EUR") -> dict:
    page = client.get(
        "/employees", params={"q": employee["email"], "reporting_currency": currency}
    ).json()
    return page["items"][0] | {"rates_as_of": page["rates_as_of"]}


# --- provider outage: reads keep working on stored rates ---


def test_failed_fetch_keeps_serving_stored_rates_with_their_date(db, client, employee):
    provider = FakeProvider(status_code=503)

    result = run_daily(db, provider)

    assert provider.calls == 1
    assert not result.ok
    expected = {"EUR": "2099-01-01", "INR": "2099-01-01"}
    shown = profile(client, employee)
    assert Decimal(shown["total_compensation_reporting"]) == Decimal("24000.00")
    assert shown["rates_as_of"] == expected
    item = directory_item(client, employee)
    assert Decimal(item["total_compensation_reporting"]) == Decimal("24000.00")
    assert item["rates_as_of"] == expected


def test_successful_fetch_is_used_by_the_next_read(db, client, employee):
    result = run_daily(db, FakeProvider())

    assert result.ok and result.rates.rate_date == date(2099, 1, 3)
    # 2.4M INR / 100 = 24k USD; 1 EUR = 2 USD -> 12k EUR
    shown = profile(client, employee)
    assert Decimal(shown["total_compensation_reporting"]) == Decimal("12000.00")
    assert shown["rates_as_of"] == {"EUR": "2099-01-03", "INR": "2099-01-03"}


# --- the job ---


def test_failed_fetch_does_not_block_promotion(db, client, employee):
    raise_day = utc_today() + timedelta(days=10)
    base_type = profile(client, employee)["current"][0]["compensation_type_id"]
    reason = db.scalar(
        text("SELECT id FROM change_reasons WHERE code = 'annual_revision'")
    )
    change = client.post(
        f"/employees/{employee['id']}/compensation",
        json={
            "compensation_type_id": base_type,
            "change_reason_id": reason,
            "effective_date": str(raise_day),
            "amount": "250000",
            "changed_by": "hr@acme",
        },
    ).json()
    assert change["is_current"] is False

    result = run_daily(db, FakeProvider(status_code=503), today=raise_day)

    assert not result.ok
    assert result.promoted >= 1  # global count: other pending records may be due too
    current = db.scalar(
        text(
            "SELECT compensation_record_id FROM current_compensation"
            " WHERE employee_id = :e AND compensation_type_id = :t"
        ),
        {"e": employee["id"], "t": base_type},
    )
    assert current == change["id"]


def test_job_does_not_refetch_within_an_hour(db, stored_rates):
    provider = FakeProvider()
    run_daily(db, provider)
    assert provider.calls == 1

    second = run_daily(db, provider)

    assert provider.calls == 1
    assert second.ok and second.rates.skipped


def test_job_refetches_after_an_hour_even_inside_the_api_throttle(db, stored_rates):
    """The API's refresh waits 24 h; the scheduled job must not skip a day because
    yesterday's run happened a few seconds later in the day."""
    db.execute(
        text(
            "UPDATE exchange_rates SET fetched_at = now() - interval '23 hours 59 min'"
        )
    )
    provider = FakeProvider()

    result = run_daily(db, provider)

    assert provider.calls == 1
    assert result.ok and not result.rates.skipped


@pytest.mark.parametrize(
    ("now", "seconds"),
    [
        (datetime(2026, 10, 5, 0, 0, tzinfo=UTC), 10 * 60),
        (datetime(2026, 10, 5, 0, 10, tzinfo=UTC), 24 * 3600),
        (datetime(2026, 10, 5, 23, 0, tzinfo=UTC), 70 * 60),
    ],
)
def test_worker_sleeps_until_next_run(now, seconds):
    assert seconds_until_next_run(now) == seconds


# --- reporting currency ---


@pytest.mark.parametrize("path", ["/employees", "/employees/{id}"])
def test_unsupported_reporting_currency_is_422(client, employee, path):
    response = client.get(
        path.format(id=employee["id"]), params={"reporting_currency": "ZZZ"}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == UNSUPPORTED_CURRENCY.format(code="ZZZ")


def test_reporting_currency_is_case_insensitive(client, employee):
    shown = profile(client, employee, currency="eur")
    assert shown["reporting_currency"] == "EUR"
    assert Decimal(shown["total_compensation_reporting"]) == Decimal("24000.00")


def test_supported_currency_without_a_rate_converts_to_null(client, employee):
    # KPW is legal tender (so supported) but the provider doesn't quote it.
    shown = profile(client, employee, currency="KPW")
    assert shown["total_compensation_reporting"] is None
    assert "KPW" not in shown["rates_as_of"]
