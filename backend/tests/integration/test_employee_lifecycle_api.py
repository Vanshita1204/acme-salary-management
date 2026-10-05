"""Phase 6 (FR-2, FR-3): create with base pay, edit, terminate, profile, current pay."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    ChangeReason,
    Company,
    CompensationRecord,
    CompensationType,
    CurrentCompensation,
    Employee,
    ExchangeRate,
)
from app.seed.change_reasons import load_change_reasons
from app.seed.compensation_types import load_compensation_types
from app.seed.reference import load_reference_data
from app.services import employees as employee_service
from app.services.compensation import utc_today
from app.services.errors import Problem, ServiceError

FAR_FUTURE = date(2099, 1, 1)
RATES = {"USD": Decimal(1), "EUR": Decimal("1.25"), "INR": Decimal("0.0125")}
HIRE = "2024-01-15"


@pytest.fixture
def catalog(db, org):
    load_reference_data(db)
    load_compensation_types(db)
    load_change_reasons(db)
    company = Company(name=f"pytest-{uuid.uuid4().hex[:8]}")
    db.add(company)
    db.add_all(
        ExchangeRate(currency=c, rate_to_usd=r, rate_date=FAR_FUTURE, source="pytest")
        for c, r in RATES.items()
    )
    db.flush()
    types = {
        (t.category, t.subtype): t.id for t in db.scalars(select(CompensationType))
    }
    return {
        "company_id": company.id,
        "base": types[("fixed", "base")],
        "bonus": types[("bonus", "annual")],
        "wellness": types[("reimbursement", "wellness")],  # outside CTC
        "reasons": {r.code: r.id for r in db.scalars(select(ChangeReason))},
        "role": org["role"],
        "org": org,
    }


def new_employee(catalog, **overrides) -> dict:
    body = {
        "company_id": catalog["company_id"],
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": f"{uuid.uuid4().hex[:10]}@pytest.example",
        **catalog["role"],
        "current_country": "IN",
        "currency": "INR",
        "hire_date": HIRE,
        "base_pay": {"amount": "200000.00", "note": "offer letter"},
        "changed_by": "hr@acme",
    }
    body.update(overrides)
    return body


@pytest.fixture
def employee(client, catalog) -> dict:
    response = client.post("/employees", json=new_employee(catalog))
    assert response.status_code == 201, response.text
    return response.json()


def add_record(client, catalog, employee, **fields):
    body = {
        "compensation_type_id": catalog["base"],
        "change_reason_id": catalog["reasons"]["annual_revision"],
        "effective_date": "2025-04-01",
        "amount": "220000.00",
        "changed_by": "hr@acme",
    } | fields
    response = client.post(
        f"/employees/{employee['id']}/compensation-records", json=body
    )
    assert response.status_code == 201, response.text
    return response.json()


def current_rows(db, employee_id):
    return {
        row.compensation_type_id: row
        for row in db.scalars(
            select(CurrentCompensation).where(
                CurrentCompensation.employee_id == employee_id
            )
        )
    }


# --- create ---


def test_create_writes_new_hire_base_pay_record_and_current_pay(
    db, client, catalog, employee
):
    records = db.scalars(
        select(CompensationRecord).where(
            CompensationRecord.employee_id == employee["id"]
        )
    ).all()

    assert len(records) == 1
    record = records[0]
    assert record.compensation_type_id == catalog["base"]
    assert record.change_reason_id == catalog["reasons"]["new_hire"]
    assert str(record.effective_date) == HIRE
    assert (record.amount, record.currency, record.country) == (
        Decimal("200000.00"),
        "INR",
        "IN",
    )
    assert (record.note, record.changed_by) == ("offer letter", "hr@acme")
    assert (
        current_rows(db, employee["id"])[catalog["base"]].compensation_record_id
        == record.id
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_pay": None},
        {"base_pay": {"amount": "0"}},
        {"base_pay": {"amount": "-1"}},
        {"changed_by": "  "},
        {"status": "terminated"},  # termination is its own operation
        {"termination_date": "2025-01-01"},
    ],
)
def test_create_cannot_skip_or_fake_base_pay(client, catalog, overrides):
    body = new_employee(catalog, **overrides)
    if overrides.get("base_pay", "keep") is None:
        del body["base_pay"]
    assert client.post("/employees", json=body).status_code == 422


def test_create_is_all_or_nothing(db, client, catalog, monkeypatch):
    """If writing the base-pay record fails, the employee row isn't kept either."""

    def failing_append(*args, **kwargs):
        raise ServiceError(Problem.INVALID, "simulated failure")

    monkeypatch.setattr(employee_service, "append_record", failing_append)
    body = new_employee(catalog)

    response = client.post("/employees", json=body)

    assert response.status_code == 422
    assert db.scalar(select(func.count()).where(Employee.email == body["email"])) == 0


# --- edit ---


def test_patch_updates_details_only(client, catalog, employee):
    response = client.patch(
        f"/employees/{employee['id']}",
        json={
            "job_title_id": catalog["org"]["titles"]["Data Engineer"],
            "job_level_id": catalog["org"]["levels"]["L4"],
            "status": "on_leave",
        },
    )

    assert response.status_code == 200
    updated = response.json()
    assert (updated["job_title"], updated["job_level"], updated["status"]) == (
        "Data Engineer",
        "L4",
        "on_leave",
    )
    assert updated["first_name"] == employee["first_name"]  # untouched fields kept


@pytest.mark.parametrize(
    "change",
    [
        {"base_pay": {"amount": "1"}},
        {"amount": "1"},
        {"currency": "USD"},  # FR-7 currency change
        {"current_country": "US"},  # FR-7 relocation
        {"hire_date": "2020-01-01"},
        {"termination_date": "2025-01-01"},
        {"status": "terminated"},
        {"code": "EMP-000001"},
    ],
)
def test_patch_cannot_change_compensation_or_controlled_fields(
    db, client, catalog, employee, change
):
    before = current_rows(db, employee["id"])[catalog["base"]].amount

    response = client.patch(f"/employees/{employee['id']}", json=change)

    assert response.status_code == 422
    db.expire_all()
    assert current_rows(db, employee["id"])[catalog["base"]].amount == before
    profile = client.get(f"/employees/{employee['id']}").json()
    assert len(profile["history"]) == 1


def test_patch_duplicate_email_is_409(client, catalog, employee):
    other = client.post("/employees", json=new_employee(catalog)).json()
    response = client.patch(
        f"/employees/{other['id']}", json={"email": employee["email"]}
    )
    assert response.status_code == 409


def test_patch_missing_employee_is_404(client):
    assert client.patch("/employees/-1", json={"first_name": "X"}).status_code == 404


# --- terminate ---


def test_terminate_sets_status_and_date_together(client, employee):
    response = client.post(
        f"/employees/{employee['id']}/terminate",
        json={"termination_date": "2025-06-30"},
    )

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["termination_date"]) == (
        "terminated",
        "2025-06-30",
    )


@pytest.mark.parametrize(
    ("body", "code", "detail"),
    [
        ({}, 422, None),  # no date
        (
            {"termination_date": "2024-01-14"},
            422,
            employee_service.TERMINATION_BEFORE_HIRE,
        ),
        (
            {"termination_date": str(utc_today() + timedelta(days=30))},
            422,
            employee_service.TERMINATION_IN_FUTURE,
        ),
    ],
)
def test_terminate_requires_a_reachable_date(client, employee, body, code, detail):
    response = client.post(f"/employees/{employee['id']}/terminate", json=body)

    assert response.status_code == code
    if detail:
        assert response.json()["detail"] == detail


def test_terminate_rejects_date_before_existing_records(client, catalog, employee):
    add_record(client, catalog, employee, effective_date="2025-04-01")

    response = client.post(
        f"/employees/{employee['id']}/terminate",
        json={"termination_date": "2025-03-31"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == employee_service.RECORDS_AFTER_TERMINATION


def test_terminate_twice_is_409_and_terminated_employees_are_read_only(
    client, employee
):
    url = f"/employees/{employee['id']}"
    client.post(f"{url}/terminate", json={"termination_date": "2025-06-30"})

    assert (
        client.post(
            f"{url}/terminate", json={"termination_date": "2025-07-01"}
        ).status_code
        == 409
    )
    assert client.patch(url, json={"first_name": "X"}).status_code == 409


def test_terminated_employee_stays_searchable(client, catalog, employee):
    client.post(
        f"/employees/{employee['id']}/terminate",
        json={"termination_date": "2025-06-30"},
    )

    page = client.get(
        "/employees", params={"q": employee["code"], "status": "terminated"}
    ).json()

    assert [i["id"] for i in page["items"]] == [employee["id"]]


def test_terminate_missing_employee_is_404(client):
    response = client.post(
        "/employees/-1/terminate", json={"termination_date": "2025-01-01"}
    )
    assert response.status_code == 404


# --- profile ---


def test_profile_breakdown_totals_and_history(client, catalog, employee):
    add_record(client, catalog, employee)  # base 200k -> 220k on 2025-04-01 (+10%)
    add_record(
        client,
        catalog,
        employee,
        compensation_type_id=catalog["bonus"],
        change_reason_id=catalog["reasons"]["bonus_payout"],
        effective_date="2025-03-15",
        amount="300000.00",
    )
    add_record(
        client,
        catalog,
        employee,
        compensation_type_id=catalog["wellness"],
        change_reason_id=catalog["reasons"]["new_hire"],
        effective_date=HIRE,
        amount="12000.00",
    )

    profile = client.get(
        f"/employees/{employee['id']}", params={"reporting_currency": "usd"}
    ).json()

    current = {c["name"]: c for c in profile["current"]}
    assert current["Base Pay"]["amount"] == "220000.00"
    assert current["Base Pay"]["annual_amount"] == "2640000.00"  # monthly x 12
    assert current["Base Pay"]["annual_amount_reporting"] == "33000.00"  # at 80 INR/USD
    assert current["Wellness Reimbursement"]["counts_toward_total"] is False
    # CTC: 2,640,000 base + 300,000 bonus; wellness is outside CTC
    assert profile["total_compensation"] == "2940000.00"
    assert profile["total_compensation_reporting"] == "36750.00"
    assert profile["reporting_currency"] == "USD"
    assert set(profile["rates_as_of"]) == {"INR", "USD"}

    history = profile["history"]
    assert [h["effective_date"] for h in history] == [
        "2025-04-01",
        "2025-03-15",
        HIRE,
        HIRE,
    ]
    raise_ = history[0]
    assert (
        raise_["change_reason"],
        raise_["previous_amount"],
        raise_["percent_change"],
    ) == (
        "annual_revision",
        "200000.00",
        "10.00",
    )
    first_records = [h for h in history if h["previous_amount"] is None]
    assert {h["compensation_type"] for h in first_records} == {
        "Base Pay",
        "Annual Bonus",
        "Wellness Reimbursement",
    }
    assert all(h["percent_change"] is None for h in first_records)
    assert all(h["currency_changed"] is False for h in history)


def test_profile_missing_employee_is_404(client):
    assert client.get("/employees/-1").status_code == 404


# --- current compensation follows records ---


def test_newer_record_moves_current_pay(db, client, catalog, employee):
    record = add_record(client, catalog, employee, effective_date="2025-04-01")
    assert (
        current_rows(db, employee["id"])[catalog["base"]].compensation_record_id
        == record["id"]
    )


def test_back_dated_record_does_not_displace_newer_current_pay(
    db, client, catalog, employee
):
    newer = add_record(client, catalog, employee, effective_date="2025-04-01")
    add_record(
        client,
        catalog,
        employee,
        effective_date="2024-06-01",
        amount="205000.00",
        change_reason_id=catalog["reasons"]["correction"],
    )
    db.expire_all()
    assert (
        current_rows(db, employee["id"])[catalog["base"]].compensation_record_id
        == newer["id"]
    )


def test_future_dated_record_is_stored_but_not_yet_current(
    db, client, catalog, employee
):
    future = str(utc_today() + timedelta(days=60))
    add_record(client, catalog, employee, effective_date=future)
    db.expire_all()
    current = current_rows(db, employee["id"])[catalog["base"]]
    assert str(current.effective_date) == HIRE
