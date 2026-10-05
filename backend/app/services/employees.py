"""Employee lifecycle (FR-2, FR-3): create with base pay, edit, terminate, profile."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.domain.compensation import (
    Component,
    annualize,
    percentage_change,
    total_compensation,
)
from app.domain.currency import MissingRateError, convert
from app.models import (
    ChangeReason,
    CompensationRecord,
    CompensationType,
    CurrentCompensation,
    Employee,
)
from app.services.compensation import (
    EMPLOYEE_NOT_FOUND,
    TERMINATED,
    append_record,
    promote_due_records,
    reason_id,
    utc_today,
)
from app.services.errors import Problem, ServiceError
from app.services.exchange_rates import latest_rates

NEW_HIRE = "new_hire"
CENT = Decimal("0.01")

NO_BASE_PAY_TYPE = "no base-pay compensation type is configured"
ALREADY_TERMINATED = "employee is already terminated"
TERMINATION_BEFORE_HIRE = "termination_date cannot be before hire_date"
TERMINATION_IN_FUTURE = "termination_date cannot be in the future"
RECORDS_AFTER_TERMINATION = (
    "employee has compensation records effective after the termination date"
)
TERMINATED_EMPLOYEE_UNEDITABLE = "terminated employees can't be edited"


def get_employee(session: Session, employee_id: int) -> Employee:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise ServiceError(Problem.NOT_FOUND, EMPLOYEE_NOT_FOUND)
    return employee


def base_pay_type_id(session: Session) -> int:
    found = session.scalar(
        select(CompensationType.id).where(CompensationType.is_base_pay)
    )
    if found is None:
        raise ServiceError(Problem.INVALID, NO_BASE_PAY_TYPE)
    return found


# --- create / edit / terminate ---


def create_employee(
    session: Session,
    fields: dict,
    *,
    base_pay_amount: Decimal,
    changed_by: str,
    note: str | None = None,
) -> Employee:
    """Create an employee and their initial base-pay record ("new hire") together.

    Both rows go in one transaction, so an employee never exists without base pay.
    The record is effective on the hire date.
    """
    employee = Employee(**fields)
    session.add(employee)
    session.flush()  # constraint violations (email, company, country...) surface here
    append_record(
        session,
        employee,
        compensation_type_id=base_pay_type_id(session),
        change_reason_id=reason_id(session, NEW_HIRE),
        effective_date=employee.hire_date,
        amount=base_pay_amount,
        changed_by=changed_by,
        note=note,
    )
    return employee


def update_employee(session: Session, employee_id: int, changes: dict) -> Employee:
    """Edit employee fields only. Compensation, country and currency have their own flows."""
    employee = get_employee(session, employee_id)
    if employee.status == TERMINATED:
        raise ServiceError(Problem.CONFLICT, TERMINATED_EMPLOYEE_UNEDITABLE)
    for name, value in changes.items():
        setattr(employee, name, value)
    session.flush()
    return employee


def terminate_employee(
    session: Session,
    employee_id: int,
    termination_date: date,
    today: date | None = None,
) -> Employee:
    """Set status and termination date together. Nothing is deleted."""
    employee = get_employee(session, employee_id)
    today = today or utc_today()
    if employee.status == TERMINATED:
        raise ServiceError(Problem.CONFLICT, ALREADY_TERMINATED)
    if termination_date < employee.hire_date:
        raise ServiceError(Problem.INVALID, TERMINATION_BEFORE_HIRE)
    if termination_date > today:
        raise ServiceError(Problem.INVALID, TERMINATION_IN_FUTURE)
    later_records = session.scalar(
        select(
            exists().where(
                CompensationRecord.employee_id == employee.id,
                CompensationRecord.effective_date > termination_date,
            )
        )
    )
    if later_records:
        raise ServiceError(Problem.INVALID, RECORDS_AFTER_TERMINATION)
    employee.status = TERMINATED
    employee.termination_date = termination_date
    session.flush()
    return employee


# --- profile ---


@dataclass(frozen=True)
class CurrentItem:
    compensation_type: CompensationType
    record_id: int
    effective_date: date
    amount: Decimal  # per period_months, employee's currency
    annual_amount: Decimal
    annual_amount_reporting: Decimal | None


@dataclass(frozen=True)
class HistoryItem:
    record: CompensationRecord
    compensation_type: CompensationType
    reason: ChangeReason
    previous_amount: Decimal | None  # previous record of the same type
    percent_change: (
        Decimal | None
    )  # None for the first record or across a currency change
    currency_changed: bool


@dataclass(frozen=True)
class Profile:
    employee: Employee
    reporting_currency: str
    rates_as_of: dict[str, date]
    total_compensation: Decimal  # annual CTC, employee's currency
    total_compensation_reporting: Decimal | None
    current: list[CurrentItem]
    history: list[HistoryItem]  # newest first


def get_profile(session: Session, employee_id: int, reporting_currency: str) -> Profile:
    employee = get_employee(session, employee_id)
    promote_due_records(session)
    reporting = reporting_currency.upper()
    rates, rate_dates = latest_rates(session)

    def to_reporting(amount: Decimal) -> Decimal | None:
        try:
            return convert(amount, employee.currency, reporting, rates)
        except MissingRateError:
            return None

    current_rows = session.execute(
        select(CurrentCompensation, CompensationType)
        .join(
            CompensationType,
            CompensationType.id == CurrentCompensation.compensation_type_id,
        )
        .where(CurrentCompensation.employee_id == employee.id)
        .order_by(
            CompensationType.is_base_pay.desc(),
            CompensationType.category,
            CompensationType.name,
        )
    ).all()
    current = [
        CurrentItem(
            compensation_type=ct,
            record_id=cc.compensation_record_id,
            effective_date=cc.effective_date,
            amount=cc.amount,
            annual_amount=annualize(cc.amount, ct.period_months).quantize(CENT),
            annual_amount_reporting=to_reporting(
                annualize(cc.amount, ct.period_months)
            ),
        )
        for cc, ct in current_rows
    ]
    total = total_compensation(
        Component(cc.amount, ct.period_months, ct.counts_toward_total)
        for cc, ct in current_rows
    ).quantize(CENT)

    return Profile(
        employee=employee,
        reporting_currency=reporting,
        rates_as_of={
            c: rate_dates[c]
            for c in sorted({employee.currency, reporting})
            if c in rate_dates
        },
        total_compensation=total,
        total_compensation_reporting=to_reporting(total),
        current=current,
        history=history(session, employee.id),
    )


def history(session: Session, employee_id: int) -> list[HistoryItem]:
    """Every record newest first, each with its change from the previous same-type record."""
    rows = session.execute(
        select(CompensationRecord, CompensationType, ChangeReason)
        .join(
            CompensationType,
            CompensationType.id == CompensationRecord.compensation_type_id,
        )
        .join(ChangeReason, ChangeReason.id == CompensationRecord.change_reason_id)
        .where(CompensationRecord.employee_id == employee_id)
        .order_by(CompensationRecord.effective_date, CompensationRecord.id)
    ).all()
    previous: dict[int, CompensationRecord] = {}
    items: list[HistoryItem] = []
    for (
        record,
        comp_type,
        reason,
    ) in rows:  # oldest first, to know each one's predecessor
        before = previous.get(comp_type.id)
        currency_changed = before is not None and before.currency != record.currency
        items.append(
            HistoryItem(
                record=record,
                compensation_type=comp_type,
                reason=reason,
                previous_amount=before.amount if before else None,
                percent_change=(
                    None
                    if before is None or currency_changed
                    else _rounded(percentage_change(before.amount, record.amount))
                ),
                currency_changed=currency_changed,
            )
        )
        previous[comp_type.id] = record
    items.reverse()
    return items


def _rounded(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(CENT)
