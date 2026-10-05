"""Phase 7 (FR-4): recording compensation changes, future-dated records, corrections."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.domain.compensation import TotalChange, average_increase
from app.models import (
    ChangeReason,
    Company,
    CompensationRecord,
    CompensationType,
    CurrentCompensation,
)
from app.seed.change_reasons import load_change_reasons
from app.seed.compensation_types import load_compensation_types
from app.seed.reference import load_reference_data
from app.services import compensation as compensation_service
from app.services.compensation import (
    AFTER_TERMINATION_DATE,
    BASE_PAY_NOT_POSITIVE,
    BEFORE_HIRE_DATE,
    UNKNOWN_CHANGE_REASON,
    UNKNOWN_COMPENSATION_TYPE,
    promote_due_records,
    utc_today,
)

HIRE = "2024-01-15"
BASE = Decimal("200000.00")  # monthly, INR


@pytest.fixture
def catalog(db):
    load_reference_data(db)
    load_compensation_types(db)
    load_change_reasons(db)
    company = Company(name=f"pytest-{uuid.uuid4().hex[:8]}")
    db.add(company)
    db.flush()
    types = {
        (t.category, t.subtype): t.id for t in db.scalars(select(CompensationType))
    }
    return {
        "company_id": company.id,
        "base": types[("fixed", "base")],
        "bonus": types[("bonus", "annual")],
        "reasons": {r.code: r.id for r in db.scalars(select(ChangeReason))},
    }


@pytest.fixture
def employee(client, catalog) -> dict:
    response = client.post(
        "/employees",
        json={
            "company_id": catalog["company_id"],
            "first_name": "Grace",
            "last_name": "Hopper",
            "email": f"{uuid.uuid4().hex[:10]}@pytest.example",
            "department": "Engineering",
            "job_title": "Engineer",
            "job_level": "L3",
            "current_country": "IN",
            "currency": "INR",
            "hire_date": HIRE,
            "base_pay": {"amount": str(BASE)},
            "changed_by": "hr@acme",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def change_body(catalog, **fields) -> dict:
    return {
        "compensation_type_id": catalog["base"],
        "change_reason_id": catalog["reasons"]["annual_revision"],
        "effective_date": "2025-04-01",
        "amount": "220000.00",
        "changed_by": "hr@acme",
    } | fields


def post_change(client, employee, body):
    return client.post(f"/employees/{employee['id']}/compensation", json=body)


def change(client, catalog, employee, **fields) -> dict:
    response = post_change(client, employee, change_body(catalog, **fields))
    assert response.status_code == 201, response.text
    return response.json()


def current_record_id(db, employee, type_id) -> int | None:
    db.expire_all()
    return db.scalar(
        select(CurrentCompensation.compensation_record_id).where(
            CurrentCompensation.employee_id == employee["id"],
            CurrentCompensation.compensation_type_id == type_id,
        )
    )


def record_count(db, employee) -> int:
    return db.scalar(
        select(func.count()).where(CompensationRecord.employee_id == employee["id"])
    )


def profile_total(client, employee) -> Decimal:
    return Decimal(
        client.get(f"/employees/{employee['id']}").json()["total_compensation"]
    )


def directory_total(client, employee) -> Decimal:
    items = client.get("/employees", params={"q": employee["email"]}).json()["items"]
    assert [i["id"] for i in items] == [employee["id"]]
    return Decimal(items[0]["total_compensation"])


@pytest.fixture
def travel_to(monkeypatch):
    """Make "today" a later date for reads (and writes) through the services."""

    def travel(day: date) -> None:
        monkeypatch.setattr(compensation_service, "utc_today", lambda: day)

    return travel


# --- recording a change ---


def test_change_appends_a_record_and_moves_current_pay(db, client, catalog, employee):
    record = change(client, catalog, employee)

    assert record["is_current"] is True
    assert (record["currency"], record["country"]) == ("INR", "IN")
    assert record_count(db, employee) == 2  # new hire + this one
    assert current_record_id(db, employee, catalog["base"]) == record["id"]
    assert profile_total(client, employee) == Decimal("2640000.00")  # 220k * 12


def test_any_reason_works_with_any_type(db, client, catalog, employee):
    record = change(
        client,
        catalog,
        employee,
        compensation_type_id=catalog["bonus"],
        change_reason_id=catalog["reasons"]["relocation"],
        amount="300000",
    )

    assert record["is_current"] is True
    assert current_record_id(db, employee, catalog["bonus"]) == record["id"]


def test_previous_records_are_never_modified(db, client, catalog, employee):
    before = db.scalars(
        select(CompensationRecord).where(
            CompensationRecord.employee_id == employee["id"]
        )
    ).one()
    snapshot = (before.id, before.amount, before.effective_date, before.created_at)

    change(client, catalog, employee)
    db.expire_all()

    after = db.get(CompensationRecord, snapshot[0])
    assert (after.id, after.amount, after.effective_date, after.created_at) == snapshot


def test_legacy_records_path_runs_the_same_rules(client, catalog, employee):
    response = client.post(
        f"/employees/{employee['id']}/compensation-records",
        json=change_body(catalog, change_reason_id=-1),
    )
    assert response.status_code == 422
    assert response.json()["detail"] == UNKNOWN_CHANGE_REASON


# --- validation, before the database ---


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"change_reason_id": -1}, UNKNOWN_CHANGE_REASON),
        ({"compensation_type_id": -1}, UNKNOWN_COMPENSATION_TYPE),
        ({"effective_date": "2024-01-14"}, BEFORE_HIRE_DATE),
        ({"amount": "0"}, BASE_PAY_NOT_POSITIVE),
    ],
)
def test_invalid_change_is_a_clear_422_and_writes_nothing(
    db, client, catalog, employee, overrides, detail
):
    response = post_change(client, employee, change_body(catalog, **overrides))

    assert response.status_code == 422
    assert response.json()["detail"] == detail
    assert record_count(db, employee) == 1


def test_zero_is_allowed_for_non_base_types(client, catalog, employee):
    record = change(
        client, catalog, employee, compensation_type_id=catalog["bonus"], amount="0"
    )
    assert record["amount"] == "0.00"


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": "-1"},
        {"amount": "1.001"},
        {"changed_by": " "},
        {"currency": "USD"},  # always the employee's current currency
        {"country": "US"},
    ],
)
def test_malformed_body_is_rejected(client, catalog, employee, overrides):
    assert (
        post_change(client, employee, change_body(catalog, **overrides)).status_code
        == 422
    )


def test_change_for_missing_employee_is_404(client, catalog):
    response = client.post("/employees/-1/compensation", json=change_body(catalog))
    assert response.status_code == 404


def test_terminated_employee_takes_no_change_after_termination(
    db, client, catalog, employee
):
    assert (
        client.post(
            f"/employees/{employee['id']}/terminate",
            json={"termination_date": "2025-06-30"},
        ).status_code
        == 200
    )

    late = post_change(
        client, employee, change_body(catalog, effective_date="2025-07-01")
    )
    assert late.status_code == 422
    assert late.json()["detail"] == AFTER_TERMINATION_DATE

    # Fixing a mistake from while they were employed is still possible.
    change(
        client,
        catalog,
        employee,
        effective_date="2025-04-01",
        change_reason_id=catalog["reasons"]["correction"],
    )


# --- future-dated changes ---


def test_future_raise_is_stored_but_not_current_until_its_date(
    db, client, catalog, employee, travel_to
):
    hire_record = current_record_id(db, employee, catalog["base"])
    raise_day = utc_today() + timedelta(days=30)

    record = change(
        client, catalog, employee, effective_date=str(raise_day), amount="250000"
    )

    assert record["is_current"] is False
    assert current_record_id(db, employee, catalog["base"]) == hire_record
    assert profile_total(client, employee) == BASE * 12
    assert directory_total(client, employee) == BASE * 12

    travel_to(raise_day - timedelta(days=1))
    assert profile_total(client, employee) == BASE * 12

    travel_to(raise_day)
    assert directory_total(client, employee) == Decimal("3000000.00")
    assert current_record_id(db, employee, catalog["base"]) == record["id"]
    assert profile_total(client, employee) == Decimal("3000000.00")


def test_promotion_picks_the_newest_due_record_and_leaves_later_ones(
    db, client, catalog, employee
):
    today = utc_today()
    first = change(
        client, catalog, employee, effective_date=str(today + timedelta(days=10))
    )
    second = change(
        client,
        catalog,
        employee,
        effective_date=str(today + timedelta(days=20)),
        amount="230000",
    )
    third = change(
        client,
        catalog,
        employee,
        effective_date=str(today + timedelta(days=40)),
        amount="240000",
    )
    assert not any(r["is_current"] for r in (first, second, third))

    # Counts are global (the dev database may hold other pending records), so only
    # check this employee's pointer.
    assert promote_due_records(db, today + timedelta(days=25)) >= 1
    assert current_record_id(db, employee, catalog["base"]) == second["id"]

    promote_due_records(db, today + timedelta(days=39))
    assert current_record_id(db, employee, catalog["base"]) == second["id"]

    assert promote_due_records(db, today + timedelta(days=40)) >= 1
    assert current_record_id(db, employee, catalog["base"]) == third["id"]


def test_promotion_is_a_no_op_once_nothing_is_due(db, client, catalog, employee):
    later = utc_today() + timedelta(days=3650)
    change(
        client, catalog, employee, effective_date=str(utc_today() + timedelta(days=10))
    )

    promote_due_records(db, later)

    assert promote_due_records(db, later) == 0


def test_promotion_does_not_displace_a_newer_current_record(
    db, client, catalog, employee, travel_to
):
    today = utc_today()
    pending = change(
        client, catalog, employee, effective_date=str(today + timedelta(days=10))
    )
    travel_to(today + timedelta(days=20))
    newer = change(
        client,
        catalog,
        employee,
        effective_date=str(today + timedelta(days=15)),
        amount="260000",
    )
    assert newer["is_current"] is True

    promote_due_records(db, today + timedelta(days=20))
    assert current_record_id(db, employee, catalog["base"]) == newer["id"]
    assert pending["id"] != newer["id"]


def test_future_dated_new_type_appears_only_on_its_date(
    db, client, catalog, employee, travel_to
):
    payout_day = utc_today() + timedelta(days=5)
    change(
        client,
        catalog,
        employee,
        compensation_type_id=catalog["bonus"],
        change_reason_id=catalog["reasons"]["bonus_payout"],
        effective_date=str(payout_day),
        amount="120000",
    )
    assert current_record_id(db, employee, catalog["bonus"]) is None

    travel_to(payout_day)
    assert profile_total(client, employee) == BASE * 12 + Decimal("120000.00")


# --- corrections ---


def test_back_dated_correction_is_recorded_but_does_not_displace_newer_pay(
    db, client, catalog, employee
):
    revision = change(client, catalog, employee, effective_date="2025-04-01")

    correction = change(
        client,
        catalog,
        employee,
        effective_date="2024-01-15",
        amount="205000",
        change_reason_id=catalog["reasons"]["correction"],
        note="offer letter said 205k",
    )

    assert correction["is_current"] is False
    assert current_record_id(db, employee, catalog["base"]) == revision["id"]


def test_corrections_do_not_count_toward_average_increase(client, catalog, employee):
    change(client, catalog, employee, effective_date="2025-04-01", amount="220000")
    change(
        client,
        catalog,
        employee,
        effective_date="2025-05-01",
        amount="240000",
        change_reason_id=catalog["reasons"]["correction"],
    )
    history = client.get(f"/employees/{employee['id']}").json()["history"]
    changes = [
        TotalChange(
            previous_total=Decimal(h["previous_amount"]) * 12,
            new_total=Decimal(h["amount"]) * 12,
            previous_currency="INR",
            new_currency=h["currency"],
            reason_code=h["change_reason"],
        )
        for h in history
        if h["previous_amount"] is not None
    ]

    assert {c.reason_code for c in changes} == {"annual_revision", "correction"}
    assert average_increase(changes) == Decimal(10)  # the 10% raise alone
