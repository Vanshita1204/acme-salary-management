"""Every rule in DATABASE_DESIGN.md's business-rules table, violated against the real schema.

Reference rows use ISO's reserved test codes (currency XTS/XXX, country XA/XB) so they can't
collide with real reference data, and everything is rolled back after each test.
"""

import re
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ChangeReason,
    Company,
    CompensationRecord,
    CompensationType,
    Country,
    Currency,
    Employee,
)

HIRE_DATE = date(2024, 1, 15)


@pytest.fixture
def ref(db: Session) -> dict:
    db.add_all(
        [
            Currency(code="XTS", name="Test currency"),
            Currency(code="XXX", name="Other test currency"),
        ]
    )
    db.flush()
    db.add_all(
        [
            Country(code="XA", name="Test country A", default_currency="XTS"),
            Country(code="XB", name="Test country B", default_currency="XXX"),
        ]
    )
    company = Company(name=f"pytest-{uuid.uuid4()}")
    db.add(company)

    # Only one base-pay type may exist globally, so reuse a seeded one if present.
    base_pay = db.scalar(select(CompensationType).where(CompensationType.is_base_pay))
    if base_pay is None:
        base_pay = CompensationType(
            category="fixed",
            subtype="base",
            period_months=1,
            is_base_pay=True,
            name="Base Pay",
        )
        db.add(base_pay)
    bonus = CompensationType(
        category=f"pytest-bonus-{uuid.uuid4()}", period_months=3, name="Quarterly Bonus"
    )
    db.add(bonus)
    db.flush()

    # Reasons are shared across types, so one reason serves every record below.
    reason = ChangeReason(code=f"pytest-{uuid.uuid4()}", label="t")
    db.add(reason)
    db.flush()
    return {
        "company": company,
        "base_pay": base_pay,
        "bonus": bonus,
        "reason": reason,
    }


def make_employee(db: Session, ref: dict, **overrides) -> Employee:
    fields = {
        "company_id": ref["company"].id,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": f"{uuid.uuid4()}@example.com",
        "department": "Engineering",
        "job_title": "Engineer",
        "job_level": "L3",
        "current_country": "XA",
        "currency": "XTS",
        "status": "active",
        "hire_date": HIRE_DATE,
    }
    fields.update(overrides)
    employee = Employee(**fields)
    db.add(employee)
    db.flush()
    return employee


def make_record(
    db: Session, ref: dict, employee: Employee, **overrides
) -> CompensationRecord:
    fields = {
        "employee_id": employee.id,
        "compensation_type_id": ref["base_pay"].id,
        "change_reason_id": ref["reason"].id,
        "effective_date": HIRE_DATE,
        "country": "XA",
        "currency": "XTS",
        "amount": Decimal("5000.00"),
        "changed_by": "pytest",
    }
    fields.update(overrides)
    record = CompensationRecord(**fields)
    db.add(record)
    db.flush()
    return record


# --- sanity: the happy path works, so the failures below are about the rule under test ---


def test_valid_employee_and_records_are_accepted(db, ref):
    employee = make_employee(db, ref)
    make_record(db, ref, employee)
    make_record(
        db,
        ref,
        employee,
        compensation_type_id=ref["bonus"].id,
        change_reason_id=ref["reason"].id,
        amount=Decimal(0),  # zero is fine for non-base-pay types
    )


def test_employee_code_is_generated_from_sequence(db, ref):
    employee = make_employee(db, ref)
    db.refresh(employee)
    assert re.fullmatch(r"EMP-\d{6}", employee.code)


# --- compensation amounts ---


@pytest.mark.parametrize("amount", ["0", "-1"])
def test_base_pay_must_be_greater_than_zero(db, ref, amount):
    employee = make_employee(db, ref)
    with pytest.raises(
        DBAPIError, match="base-pay compensation must be greater than zero"
    ):
        make_record(db, ref, employee, amount=Decimal(amount))


def test_other_components_cannot_be_negative(db, ref):
    employee = make_employee(db, ref)
    with pytest.raises(IntegrityError, match="chk_comp_record_amount_non_negative"):
        make_record(
            db,
            ref,
            employee,
            compensation_type_id=ref["bonus"].id,
            change_reason_id=ref["reason"].id,
            amount=Decimal("-0.01"),
        )


def test_effective_date_cannot_precede_hire_date(db, ref):
    employee = make_employee(db, ref)
    with pytest.raises(DBAPIError, match="effective_date cannot precede hire_date"):
        make_record(db, ref, employee, effective_date=date(2024, 1, 14))


def test_new_record_must_use_employees_current_currency(db, ref):
    employee = make_employee(db, ref)
    with pytest.raises(DBAPIError, match="must use employee .* current currency"):
        make_record(db, ref, employee, currency="XXX")


def test_change_reason_must_exist(db, ref):
    employee = make_employee(db, ref)
    with pytest.raises(
        IntegrityError, match="compensation_records_change_reason_id_fkey"
    ):
        make_record(db, ref, employee, change_reason_id=-1)


def test_change_reason_codes_are_unique(db, ref):
    db.add(ChangeReason(code=ref["reason"].code, label="duplicate"))
    with pytest.raises(IntegrityError, match="change_reasons_code_key"):
        db.flush()


# --- compensation types ---


def test_only_one_base_pay_type_exists_globally(db, ref):
    db.add(
        CompensationType(
            category=f"pytest-{uuid.uuid4()}",
            period_months=1,
            is_base_pay=True,
            name="Base 2",
        )
    )
    with pytest.raises(IntegrityError, match="uq_one_base_pay_type"):
        db.flush()


def test_base_pay_type_must_count_toward_total(db, ref):
    ref["base_pay"].counts_toward_total = False
    with pytest.raises(IntegrityError, match="chk_base_pay_counts_toward_total"):
        db.flush()


@pytest.mark.parametrize("period_months", [0, -3])
def test_period_months_must_be_positive(db, period_months):
    db.add(
        CompensationType(
            category=f"pytest-{uuid.uuid4()}", period_months=period_months, name="x"
        )
    )
    with pytest.raises(IntegrityError, match="chk_period_months_positive"):
        db.flush()


# --- employees ---


def test_termination_date_requires_terminated_status(db, ref):
    with pytest.raises(IntegrityError, match="chk_termination_date_matches_status"):
        make_employee(db, ref, status="active", termination_date=date(2025, 1, 1))


def test_terminated_status_requires_termination_date(db, ref):
    with pytest.raises(IntegrityError, match="chk_termination_date_matches_status"):
        make_employee(db, ref, status="terminated")


def test_termination_cannot_precede_hire(db, ref):
    with pytest.raises(IntegrityError, match="chk_termination_after_hire"):
        make_employee(db, ref, status="terminated", termination_date=date(2023, 12, 31))


def test_status_must_be_known(db, ref):
    with pytest.raises(IntegrityError, match="chk_employee_status"):
        make_employee(db, ref, status="retired")


def test_email_is_unique_case_insensitively(db, ref):
    make_employee(db, ref, email="ada@example.com")
    with pytest.raises(IntegrityError, match="employees_email_key"):
        make_employee(db, ref, email="ADA@Example.com")


@pytest.mark.parametrize(
    ("n", "code"),
    [
        (42, "EMP-000042"),
        (999_999, "EMP-999999"),
        (1_000_000, "EMP-1000000"),  # lpad alone would truncate this to EMP-100000
        (12_345_678, "EMP-12345678"),
    ],
)
def test_employee_code_pads_but_never_truncates(db, n, code):
    assert db.scalar(text("SELECT employee_code(:n)"), {"n": n}) == code


def test_employee_code_is_unique(db, ref):
    first = make_employee(db, ref)
    db.refresh(first)
    with pytest.raises(IntegrityError, match="employees_code_key"):
        make_employee(db, ref, code=first.code)


def test_currency_must_be_supported(db, ref):
    with pytest.raises(IntegrityError, match="employees_currency_fkey"):
        make_employee(db, ref, currency="XYZ")


def test_country_must_be_valid(db, ref):
    with pytest.raises(IntegrityError, match="employees_current_country_fkey"):
        make_employee(db, ref, current_country="QQ")


# --- append-only history ---


def test_compensation_records_cannot_be_updated(db, ref):
    record = make_record(db, ref, make_employee(db, ref))
    with pytest.raises(DBAPIError, match="compensation_records is append-only"):
        db.execute(
            text("UPDATE compensation_records SET amount = 1 WHERE id = :id"),
            {"id": record.id},
        )


def test_compensation_records_cannot_be_deleted(db, ref):
    record = make_record(db, ref, make_employee(db, ref))
    with pytest.raises(DBAPIError, match="compensation_records is append-only"):
        db.execute(
            text("DELETE FROM compensation_records WHERE id = :id"), {"id": record.id}
        )
