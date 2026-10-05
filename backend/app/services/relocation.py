"""Country and currency changes (FR-7).

Relocation updates the employee's country and writes a dated "relocation" record for
base pay, so the move appears in compensation history even when no amount changes.
A currency change rewrites every current compensation type in the new currency, with
amounts HR enters (never converted automatically), in the same transaction that
changes `employees.currency` — so no type is ever left current in the old currency.
A relocation can carry a currency change; then it's one step, with every record
written as "relocation".

Every record goes through Phase 7's `record_change`, after the employee row has been
updated (the database trigger requires a new record's currency to match the
employee's).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models import (
    CompensationRecord,
    CompensationType,
    Country,
    Currency,
    CurrentCompensation,
    Employee,
)
from app.services.compensation import (
    TERMINATED,
    CompensationChange,
    reason_id,
    record_change,
    utc_today,
)
from app.services.employees import get_employee
from app.services.errors import Problem, ServiceError

RELOCATION = "relocation"

TERMINATED_EMPLOYEE = "terminated employees can't be relocated or change currency"
UNKNOWN_COUNTRY = "country does not exist"
SAME_COUNTRY = "employee is already based in {country}"
UNKNOWN_CURRENCY = "currency does not exist"
SAME_CURRENCY = "employee is already paid in {currency}"
NO_CURRENT_BASE_PAY = "employee has no current base pay yet"
DATE_IN_FUTURE = "effective_date can't be in the future"
DATE_BEFORE_CURRENT = (
    "effective_date can't be before the current {type} record ({date}); "
    "it would be written behind it and never become current"
)
PENDING_RECORDS = (
    "employee has future-dated compensation changes; they'd become current in the "
    "old currency. Record the currency change after they take effect"
)
MISSING_AMOUNTS = "amounts are missing for current compensation types: {ids}"
EXTRA_AMOUNTS = "amounts given for types the employee doesn't currently have: {ids}"
DUPLICATE_AMOUNTS = "more than one amount for compensation types: {ids}"


@dataclass(frozen=True)
class NewAmount:
    compensation_type_id: int
    amount: Decimal


@dataclass(frozen=True)
class ChangeResult:
    employee: Employee
    records: list[CompensationChange]


def _current_rows(session: Session, employee: Employee) -> list[CurrentCompensation]:
    return list(
        session.scalars(
            select(CurrentCompensation)
            .where(CurrentCompensation.employee_id == employee.id)
            .order_by(CurrentCompensation.compensation_type_id)
        )
    )


def _check_effective_date(
    session: Session,
    rows: list[CurrentCompensation],
    effective_date: date,
    today: date,
) -> None:
    """Changes take effect now or in the past, and never behind a current record:
    the new records must become current immediately, or the employee row (already
    moved) and current pay would disagree."""
    if effective_date > today:
        raise ServiceError(Problem.INVALID, DATE_IN_FUTURE)
    for row in rows:
        if effective_date < row.effective_date:
            name = session.get(CompensationType, row.compensation_type_id).name
            raise ServiceError(
                Problem.INVALID,
                DATE_BEFORE_CURRENT.format(type=name, date=row.effective_date),
            )


def _check_new_currency(
    session: Session, employee: Employee, currency: str, today: date
) -> None:
    if session.get(Currency, currency) is None:
        raise ServiceError(Problem.INVALID, UNKNOWN_CURRENCY)
    if currency == employee.currency:
        raise ServiceError(Problem.INVALID, SAME_CURRENCY.format(currency=currency))
    pending = session.scalar(
        select(
            exists().where(
                CompensationRecord.employee_id == employee.id,
                CompensationRecord.effective_date > today,
            )
        )
    )
    if pending:
        raise ServiceError(Problem.CONFLICT, PENDING_RECORDS)


def _check_amounts_cover(
    rows: list[CurrentCompensation], amounts: list[NewAmount]
) -> None:
    """One amount for every current type, and nothing else."""
    given = [a.compensation_type_id for a in amounts]
    duplicates = sorted({t for t in given if given.count(t) > 1})
    if duplicates:
        raise ServiceError(Problem.INVALID, DUPLICATE_AMOUNTS.format(ids=duplicates))
    current = {row.compensation_type_id for row in rows}
    if missing := sorted(current - set(given)):
        raise ServiceError(Problem.INVALID, MISSING_AMOUNTS.format(ids=missing))
    if extra := sorted(set(given) - current):
        raise ServiceError(Problem.INVALID, EXTRA_AMOUNTS.format(ids=extra))


def _editable_employee(session: Session, employee_id: int) -> Employee:
    employee = get_employee(session, employee_id)
    if employee.status == TERMINATED:
        raise ServiceError(Problem.CONFLICT, TERMINATED_EMPLOYEE)
    return employee


def _rewrite_all(
    session: Session,
    employee: Employee,
    amounts: list[NewAmount],
    *,
    change_reason_id: int,
    effective_date: date,
    changed_by: str,
    note: str | None,
    today: date,
) -> list[CompensationChange]:
    changes = [
        record_change(
            session,
            employee.id,
            compensation_type_id=a.compensation_type_id,
            change_reason_id=change_reason_id,
            effective_date=effective_date,
            amount=a.amount,
            changed_by=changed_by,
            note=note,
            today=today,
        )
        for a in sorted(amounts, key=lambda a: a.compensation_type_id)
    ]
    # Guaranteed by the date checks; if it ever fails, roll everything back rather
    # than leave a type current in the old currency.
    if not all(c.is_current for c in changes):
        raise RuntimeError("a rewritten compensation type didn't become current")
    return changes


def change_currency(
    session: Session,
    employee_id: int,
    *,
    currency: str,
    amounts: list[NewAmount],
    change_reason_id: int,
    effective_date: date,
    changed_by: str,
    note: str | None = None,
    today: date | None = None,
) -> ChangeResult:
    """Switch the employee's pay currency, re-recording every current type in it."""
    today = today or utc_today()
    employee = _editable_employee(session, employee_id)
    currency = currency.upper()
    _check_new_currency(session, employee, currency, today)
    rows = _current_rows(session, employee)
    _check_amounts_cover(rows, amounts)
    _check_effective_date(session, rows, effective_date, today)

    employee.currency = currency
    session.flush()  # the record trigger compares against the employee's currency
    changes = _rewrite_all(
        session,
        employee,
        amounts,
        change_reason_id=change_reason_id,
        effective_date=effective_date,
        changed_by=changed_by,
        note=note,
        today=today,
    )
    return ChangeResult(employee=employee, records=changes)


def relocate(
    session: Session,
    employee_id: int,
    *,
    country: str,
    effective_date: date,
    changed_by: str,
    note: str | None = None,
    currency: str | None = None,
    amounts: list[NewAmount] | None = None,
    today: date | None = None,
) -> ChangeResult:
    """Move the employee to `country`, recorded as a "relocation" record.

    Without a currency, base pay is re-recorded at its current amount and currency.
    With one, every current type is re-recorded in it with the given amounts, all
    as "relocation", in the same transaction.
    """
    today = today or utc_today()
    employee = _editable_employee(session, employee_id)
    country = country.upper()
    if session.get(Country, country) is None:
        raise ServiceError(Problem.INVALID, UNKNOWN_COUNTRY)
    if country == employee.current_country:
        raise ServiceError(Problem.INVALID, SAME_COUNTRY.format(country=country))

    rows = _current_rows(session, employee)
    base_type_id = session.scalar(
        select(CompensationType.id).where(CompensationType.is_base_pay)
    )
    base = next((r for r in rows if r.compensation_type_id == base_type_id), None)
    if base is None:
        raise ServiceError(Problem.CONFLICT, NO_CURRENT_BASE_PAY)

    if currency is not None:
        currency = currency.upper()
        _check_new_currency(session, employee, currency, today)
        _check_amounts_cover(rows, amounts or [])
        _check_effective_date(session, rows, effective_date, today)
    else:
        # Same amount as now: only valid at or after the current base-pay record,
        # or it would rewrite history with today's figure.
        _check_effective_date(session, [base], effective_date, today)
        amounts = [NewAmount(base.compensation_type_id, base.amount)]

    employee.current_country = country
    if currency is not None:
        employee.currency = currency
    session.flush()
    changes = _rewrite_all(
        session,
        employee,
        amounts,
        change_reason_id=reason_id(session, RELOCATION),
        effective_date=effective_date,
        changed_by=changed_by,
        note=note,
        today=today,
    )
    return ChangeResult(employee=employee, records=changes)
