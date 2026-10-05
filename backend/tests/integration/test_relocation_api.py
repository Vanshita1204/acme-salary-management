"""Phase 11 (FR-7): relocation and currency changes."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    ChangeReason,
    CompensationRecord,
    CompensationType,
    CurrentCompensation,
    Employee,
)
from app.seed.change_reasons import load_change_reasons
from app.seed.compensation_types import load_compensation_types
from app.seed.reference import load_reference_data
from app.services import relocation
from app.services.compensation import utc_today
from app.services.errors import Problem, ServiceError

HIRE = "2024-01-15"
RAISE = "2025-04-01"


@pytest.fixture
def catalog(db, org):
    load_reference_data(db)
    load_compensation_types(db)
    load_change_reasons(db)
    types = {
        (t.category, t.subtype): t.id for t in db.scalars(select(CompensationType))
    }
    return {
        "base": types[("fixed", "base")],
        "housing": types[("allowance", "housing")],
        "bonus": types[("bonus", "annual")],
        "reasons": {r.code: r.id for r in db.scalars(select(ChangeReason))},
    }


@pytest.fixture
def employee(client, catalog, org) -> dict:
    """Based in India, paid in INR: base pay (raised once), housing and a bonus."""
    company = client.post("/companies", json={"name": f"pytest {uuid.uuid4()}"})
    response = client.post(
        "/employees",
        json={
            "company_id": company.json()["id"],
            "first_name": "Ravi",
            "last_name": "Kumar",
            "email": f"{uuid.uuid4().hex[:10]}@pytest.example",
            **org["role"],
            "current_country": "IN",
            "currency": "INR",
            "hire_date": HIRE,
            "base_pay": {"amount": "200000"},
            "changed_by": "hr@acme",
        },
    )
    assert response.status_code == 201, response.text
    employee = response.json()
    for type_key, amount, effective in (
        ("base", "220000", RAISE),
        ("housing", "30000", HIRE),
        ("bonus", "250000", RAISE),
    ):
        added = client.post(
            f"/employees/{employee['id']}/compensation",
            json={
                "compensation_type_id": catalog[type_key],
                "change_reason_id": catalog["reasons"]["annual_revision"],
                "effective_date": effective,
                "amount": amount,
                "changed_by": "hr@acme",
            },
        )
        assert added.status_code == 201, added.text
    return employee


def profile(client, employee) -> dict:
    return client.get(f"/employees/{employee['id']}").json()


def record_count(db, employee) -> int:
    return db.scalar(
        select(func.count()).where(CompensationRecord.employee_id == employee["id"])
    )


def stranded(db, employee) -> list:
    """Current compensation rows whose record isn't in the employee's currency."""
    db.expire_all()
    return db.execute(
        select(CurrentCompensation.compensation_type_id)
        .join(
            CompensationRecord,
            CompensationRecord.id == CurrentCompensation.compensation_record_id,
        )
        .join(Employee, Employee.id == CurrentCompensation.employee_id)
        .where(
            Employee.id == employee["id"],
            CompensationRecord.currency != Employee.currency,
        )
    ).all()


def relocate(client, employee, **body):
    return client.post(
        f"/employees/{employee['id']}/relocate",
        json={
            "country": "GB",
            "effective_date": str(utc_today()),
            "changed_by": "hr@acme",
        }
        | body,
    )


def usd_amounts(catalog, **overrides) -> list[dict]:
    amounts = {"base": "2600", "housing": "300", "bonus": "3000"} | overrides
    return [
        {"compensation_type_id": catalog[key], "amount": value}
        for key, value in amounts.items()
    ]


def change_currency(client, employee, catalog, **body):
    return client.post(
        f"/employees/{employee['id']}/change-currency",
        json={
            "currency": "USD",
            "amounts": usd_amounts(catalog),
            "change_reason_id": catalog["reasons"]["market_adjustment"],
            "effective_date": str(utc_today()),
            "changed_by": "hr@acme",
        }
        | body,
    )


# --- relocation, country only ---


def test_relocation_is_in_history_without_changing_any_amount(
    db, client, catalog, employee
):
    before = profile(client, employee)

    response = relocate(client, employee, note="moved to London office")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["employee"]["current_country"], body["employee"]["currency"]) == (
        "GB",
        "INR",
    )
    [record] = body["records"]
    assert (record["compensation_type_id"], record["amount"]) == (
        catalog["base"],
        "220000.00",
    )
    assert (record["country"], record["currency"], record["is_current"]) == (
        "GB",
        "INR",
        True,
    )
    after = profile(client, employee)
    top = after["history"][0]
    assert (top["change_reason"], top["note"]) == (
        "relocation",
        "moved to London office",
    )
    assert (top["percent_change"], top["currency_changed"]) == ("0.00", False)
    assert after["total_compensation"] == before["total_compensation"]
    assert len(after["history"]) == len(before["history"]) + 1
    # Earlier records keep the country they were written in.
    assert {h["country"] for h in after["history"][1:]} == {"IN"}


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"country": "IN"}, 422, relocation.SAME_COUNTRY.format(country="IN")),
        ({"country": "QQ"}, 422, relocation.UNKNOWN_COUNTRY),
        ({"effective_date": "2025-03-31"}, 422, None),  # behind the current base pay
        ({"effective_date": "2099-01-01"}, 422, relocation.DATE_IN_FUTURE),
        ({"currency": "USD"}, 422, None),  # currency without amounts
    ],
)
def test_invalid_relocation_changes_nothing(db, client, employee, body, status, detail):
    before = record_count(db, employee)

    response = relocate(client, employee, **body)

    assert response.status_code == status, response.text
    if detail:
        assert response.json()["detail"] == detail
    db.expire_all()
    assert db.get(Employee, employee["id"]).current_country == "IN"
    assert record_count(db, employee) == before


def test_relocation_can_be_back_dated_to_the_current_base_pay(client, employee):
    response = relocate(client, employee, effective_date=RAISE)
    assert response.status_code == 200, response.text
    assert response.json()["records"][0]["is_current"] is True


# --- currency change ---


def test_currency_change_rewrites_every_current_type(db, client, catalog, employee):
    before = record_count(db, employee)

    response = change_currency(client, employee, catalog)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["employee"]["currency"], body["employee"]["current_country"]) == (
        "USD",
        "IN",
    )
    assert {r["compensation_type_id"] for r in body["records"]} == {
        catalog["base"],
        catalog["housing"],
        catalog["bonus"],
    }
    assert all(r["currency"] == "USD" and r["is_current"] for r in body["records"])
    assert record_count(db, employee) == before + 3
    assert stranded(db, employee) == []
    # 2,600 x 12 + 300 x 12 + 3,000 (annual bonus) = 37,800 USD
    shown = profile(client, employee)
    assert shown["total_compensation"] == "37800.00"


def test_history_flags_the_currency_change_instead_of_a_percent(
    client, catalog, employee
):
    change_currency(client, employee, catalog)

    rewritten = [
        h for h in profile(client, employee)["history"] if h["currency"] == "USD"
    ]
    assert len(rewritten) == 3
    for item in rewritten:
        assert item["currency_changed"] is True
        assert item["percent_change"] is None
        assert item["previous_amount"] is not None  # the INR amount, for reference


@pytest.mark.parametrize(
    ("amounts", "detail"),
    [
        (
            lambda c: usd_amounts(c)[:2],
            lambda c: relocation.MISSING_AMOUNTS.format(ids=[c["bonus"]]),
        ),
        (
            lambda c: (
                usd_amounts(c) + [{"compensation_type_id": c["base"], "amount": "1"}]
            ),
            lambda c: relocation.DUPLICATE_AMOUNTS.format(ids=[c["base"]]),
        ),
        (lambda c: usd_amounts(c, base="0"), lambda c: None),  # base pay must be > 0
    ],
)
def test_currency_change_must_cover_every_type_or_changes_nothing(
    db, client, catalog, employee, amounts, detail
):
    before = record_count(db, employee)

    response = change_currency(client, employee, catalog, amounts=amounts(catalog))

    assert response.status_code == 422, response.text
    if detail(catalog):
        assert response.json()["detail"] == detail(catalog)
    db.expire_all()
    assert db.get(Employee, employee["id"]).currency == "INR"
    assert record_count(db, employee) == before
    assert stranded(db, employee) == []


def test_amount_for_a_type_the_employee_does_not_have_is_rejected(
    db, client, catalog, employee
):
    meal = db.scalar(
        select(CompensationType.id).where(CompensationType.subtype == "meal")
    )
    amounts = usd_amounts(catalog) + [{"compensation_type_id": meal, "amount": "50"}]

    response = change_currency(client, employee, catalog, amounts=amounts)

    assert response.status_code == 422
    assert response.json()["detail"] == relocation.EXTRA_AMOUNTS.format(ids=[meal])


@pytest.mark.parametrize(
    ("currency", "detail"),
    [
        ("INR", relocation.SAME_CURRENCY.format(currency="INR")),
        ("XYZ", relocation.UNKNOWN_CURRENCY),
    ],
)
def test_currency_must_be_new_and_known(client, catalog, employee, currency, detail):
    response = change_currency(client, employee, catalog, currency=currency)
    assert response.status_code == 422
    assert response.json()["detail"] == detail


def test_pending_future_change_blocks_a_currency_change(client, catalog, employee):
    client.post(
        f"/employees/{employee['id']}/compensation",
        json={
            "compensation_type_id": catalog["base"],
            "change_reason_id": catalog["reasons"]["annual_revision"],
            "effective_date": str(utc_today() + timedelta(days=30)),
            "amount": "240000",
            "changed_by": "hr@acme",
        },
    )

    response = change_currency(client, employee, catalog)

    assert response.status_code == 409
    assert response.json()["detail"] == relocation.PENDING_RECORDS


def test_failure_part_way_rolls_back_the_currency_and_records(
    db, client, catalog, employee, monkeypatch
):
    before = record_count(db, employee)
    real = relocation.record_change
    calls = []

    def fail_on_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ServiceError(Problem.INVALID, "simulated failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(relocation, "record_change", fail_on_second)

    response = change_currency(client, employee, catalog)

    assert response.status_code == 422
    db.expire_all()
    assert db.get(Employee, employee["id"]).currency == "INR"
    assert record_count(db, employee) == before
    assert stranded(db, employee) == []


# --- relocation with a currency change ---


def test_relocation_with_currency_change_is_one_step(db, client, catalog, employee):
    response = relocate(
        client,
        employee,
        country="US",
        currency="USD",
        amounts=usd_amounts(catalog),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["employee"]["current_country"], body["employee"]["currency"]) == (
        "US",
        "USD",
    )
    assert len(body["records"]) == 3
    assert {(r["country"], r["currency"]) for r in body["records"]} == {("US", "USD")}
    history = profile(client, employee)["history"]
    assert {h["change_reason"] for h in history[:3]} == {"relocation"}
    assert stranded(db, employee) == []


def test_terminated_employee_cannot_move_or_change_currency(client, catalog, employee):
    client.post(
        f"/employees/{employee['id']}/terminate",
        json={"termination_date": str(utc_today())},
    )

    assert relocate(client, employee).status_code == 409
    assert change_currency(client, employee, catalog).status_code == 409


def test_edit_still_cannot_change_country_or_currency(client, employee):
    url = f"/employees/{employee['id']}"
    assert client.patch(url, json={"current_country": "GB"}).status_code == 422
    assert client.patch(url, json={"currency": "USD"}).status_code == 422


def test_missing_employee_is_404(client, catalog):
    assert relocate(client, {"id": -1}).status_code == 404
    assert change_currency(client, {"id": -1}, catalog).status_code == 404


def test_amounts_cannot_be_negative(client, catalog, employee):
    response = change_currency(
        client, employee, catalog, amounts=usd_amounts(catalog, housing="-1")
    )
    assert response.status_code == 422
    assert profile(client, employee)["employee"]["currency"] == "INR"
