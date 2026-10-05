"""Writing compensation records and keeping `current_compensation` in step with them.

Every record is appended (history is append-only). The current pointer for its
(employee, type) moves only when the record is in effect today or earlier and is newer
than what's current. Future-dated records are stored but don't move the pointer until
their date arrives: reads of current compensation call `promote_due_records` first
(recompute-on-read, §7 — no background job at this scale).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import and_, exists, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.orm import Session

from app.models import (
    ChangeReason,
    CompensationRecord,
    CompensationType,
    CurrentCompensation,
    Employee,
)
from app.services.errors import Problem, ServiceError

TERMINATED = "terminated"

# Same wording as the database's own rejections (triggers, FK messages), so a rule
# reads the same whichever layer catches it.
EMPLOYEE_NOT_FOUND = "employee not found"
UNKNOWN_COMPENSATION_TYPE = "compensation type does not exist"
UNKNOWN_CHANGE_REASON = "change reason does not exist"
BEFORE_HIRE_DATE = "effective_date cannot precede hire_date"
AFTER_TERMINATION_DATE = "effective_date cannot be after the termination_date"
BASE_PAY_NOT_POSITIVE = "base-pay compensation must be greater than zero"

# Matches the partial index ix_comp_records_future_dated: records dated after the day
# they were written. Only these can become current later.
WRITTEN_FUTURE_DATED = text(
    "compensation_records.effective_date"
    " > (compensation_records.created_at AT TIME ZONE 'UTC')::date"
)


def utc_today() -> date:
    return datetime.now(UTC).date()


def reason_id(session: Session, code: str) -> int:
    """Id of a change reason the rules depend on (seeded and smoke-checked in 1.2)."""
    found = session.scalar(select(ChangeReason.id).where(ChangeReason.code == code))
    if found is None:
        raise RuntimeError(
            f"required change reason {code!r} is missing; run the 1.2 seeds"
        )
    return found


@dataclass(frozen=True)
class CompensationChange:
    record: CompensationRecord
    is_current: bool  # False while future-dated, or when a newer record is current


def record_change(
    session: Session,
    employee_id: int,
    *,
    compensation_type_id: int,
    change_reason_id: int,
    effective_date: date,
    amount: Decimal,
    changed_by: str,
    note: str | None = None,
    today: date | None = None,
) -> CompensationChange:
    """Record a compensation change for one type (FR-4).

    Checks the rules up front for clear errors (the database still enforces them).
    Any reason works with any type, including "correction"; future dates are allowed.
    """
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise ServiceError(Problem.NOT_FOUND, EMPLOYEE_NOT_FOUND)
    comp_type = session.get(CompensationType, compensation_type_id)
    if comp_type is None:
        raise ServiceError(Problem.INVALID, UNKNOWN_COMPENSATION_TYPE)
    if session.get(ChangeReason, change_reason_id) is None:
        raise ServiceError(Problem.INVALID, UNKNOWN_CHANGE_REASON)
    if effective_date < employee.hire_date:
        raise ServiceError(Problem.INVALID, BEFORE_HIRE_DATE)
    if employee.status == TERMINATED and effective_date > employee.termination_date:
        raise ServiceError(Problem.INVALID, AFTER_TERMINATION_DATE)
    if comp_type.is_base_pay and amount <= 0:
        raise ServiceError(Problem.INVALID, BASE_PAY_NOT_POSITIVE)

    record = append_record(
        session,
        employee,
        compensation_type_id=compensation_type_id,
        change_reason_id=change_reason_id,
        effective_date=effective_date,
        amount=amount,
        changed_by=changed_by,
        note=note,
        today=today,
    )
    current_id = session.scalar(
        select(CurrentCompensation.compensation_record_id).where(
            CurrentCompensation.employee_id == employee.id,
            CurrentCompensation.compensation_type_id == compensation_type_id,
        )
    )
    return CompensationChange(record=record, is_current=current_id == record.id)


def append_record(
    session: Session,
    employee: Employee,
    *,
    compensation_type_id: int,
    change_reason_id: int,
    effective_date: date,
    amount,
    changed_by: str,
    note: str | None = None,
    today: date | None = None,
) -> CompensationRecord:
    """Insert a record in the employee's current country and currency; update current pay."""
    record = CompensationRecord(
        employee_id=employee.id,
        compensation_type_id=compensation_type_id,
        change_reason_id=change_reason_id,
        effective_date=effective_date,
        amount=amount,
        country=employee.current_country,
        currency=employee.currency,
        changed_by=changed_by,
        note=note,
    )
    session.add(record)
    session.flush()  # DB triggers validate here (hire date, currency, base pay > 0)
    refresh_current(session, record, today or utc_today())
    return record


def refresh_current(session: Session, record: CompensationRecord, today: date) -> None:
    """Point current compensation at `record` if it's in effect and the newest."""
    if record.effective_date > today:
        return
    stmt = insert(CurrentCompensation).values(
        employee_id=record.employee_id,
        compensation_type_id=record.compensation_type_id,
        compensation_record_id=record.id,
        effective_date=record.effective_date,
        amount=record.amount,
    )
    excluded = stmt.excluded
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["employee_id", "compensation_type_id"],
            set_={
                "compensation_record_id": excluded.compensation_record_id,
                "effective_date": excluded.effective_date,
                "amount": excluded.amount,
                "updated_at": func.now(),
            },
            # Only replace an older pointer; a back-dated correction of an older
            # period doesn't displace a newer current record.
            where=tuple_(
                CurrentCompensation.effective_date,
                CurrentCompensation.compensation_record_id,
            )
            < tuple_(excluded.effective_date, excluded.compensation_record_id),
        )
    )


def promote_due_records(session: Session, today: date | None = None) -> int:
    """Make future-dated records whose date has arrived current. Returns rows moved.

    Called before reading current compensation. Only records written future-dated
    are candidates (a small partial index), and only those newer than the current
    pointer, so once everything due is promoted this is a no-op read.
    """
    today = today or utc_today()
    record = CompensationRecord
    current = CurrentCompensation
    due = (
        select(
            record.employee_id,
            record.compensation_type_id,
            record.id,
            record.effective_date,
            record.amount,
            func.now(),
        )
        .where(
            WRITTEN_FUTURE_DATED,
            record.effective_date <= today,
            ~exists().where(
                and_(
                    current.employee_id == record.employee_id,
                    current.compensation_type_id == record.compensation_type_id,
                    tuple_(current.effective_date, current.compensation_record_id)
                    >= tuple_(record.effective_date, record.id),
                )
            ),
        )
        # The newest due record per (employee, type).
        .ext(distinct_on(record.employee_id, record.compensation_type_id))
        .order_by(
            record.employee_id,
            record.compensation_type_id,
            record.effective_date.desc(),
            record.id.desc(),
        )
    )
    stmt = insert(current).from_select(
        [
            "employee_id",
            "compensation_type_id",
            "compensation_record_id",
            "effective_date",
            "amount",
            "updated_at",
        ],
        due,
    )
    excluded = stmt.excluded
    moved = session.scalars(
        stmt.on_conflict_do_update(
            index_elements=["employee_id", "compensation_type_id"],
            set_={
                "compensation_record_id": excluded.compensation_record_id,
                "effective_date": excluded.effective_date,
                "amount": excluded.amount,
                "updated_at": func.now(),
            },
            where=tuple_(current.effective_date, current.compensation_record_id)
            < tuple_(excluded.effective_date, excluded.compensation_record_id),
        ).returning(current.compensation_record_id)
    ).all()
    return len(moved)
