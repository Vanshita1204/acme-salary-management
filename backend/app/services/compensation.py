"""Writing compensation records and keeping `current_compensation` in step with them.

Every record is appended (history is append-only). The current pointer for its
(employee, type) moves only when the record is in effect today or earlier and is newer
than what's current. Future-dated records are stored but don't move the pointer;
Phase 7 resolves them once their date arrives.
"""

from datetime import UTC, date, datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import ChangeReason, CompensationRecord, CurrentCompensation, Employee


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
