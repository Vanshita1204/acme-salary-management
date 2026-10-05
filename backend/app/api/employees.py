from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.db_errors import translate_db_errors
from app.api.deps import DbSession, get_or_404
from app.domain.pagination import CursorError
from app.models import CompensationRecord, Employee
from app.schemas.employees import (
    CompensationRecordIn,
    CompensationRecordOut,
    CurrentCompensationOut,
    DirectoryItem,
    DirectoryPageOut,
    EmployeeIn,
    EmployeeOut,
    EmployeeUpdate,
    HistoryItemOut,
    ProfileOut,
    Status,
    TerminateIn,
)
from app.services import employees as employee_service
from app.services.compensation import append_record
from app.services.directory import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    DirectoryQuery,
    list_employees,
)

router = APIRouter(tags=["employees"])


@router.post(
    "/employees", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED
)
def create_employee(body: EmployeeIn, db: DbSession) -> Employee:
    """Create an employee with their initial base pay ("new hire"), atomically (FR-3)."""
    fields = body.model_dump(exclude={"base_pay", "changed_by"})
    with translate_db_errors(db):
        employee = employee_service.create_employee(
            db,
            fields,
            base_pay_amount=body.base_pay.amount,
            changed_by=body.changed_by,
            note=body.base_pay.note,
        )
        db.commit()
    db.refresh(employee)  # load the DB-generated code and timestamps
    return employee


@router.get("/employees", response_model=DirectoryPageOut)
def list_directory(
    db: DbSession,
    q: Annotated[
        str | None, Query(description="Search name, email or employee code")
    ] = None,
    department: Annotated[list[str] | None, Query()] = None,
    country: Annotated[list[str] | None, Query()] = None,
    job_title: Annotated[list[str] | None, Query()] = None,
    job_level: Annotated[list[str] | None, Query()] = None,
    status_: Annotated[list[Status] | None, Query(alias="status")] = None,
    sort: Literal["name", "hire_date", "compensation"] = "name",
    order: Literal["asc", "desc"] = "asc",
    cursor: Annotated[
        str | None, Query(description="next_cursor or prev_cursor")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    reporting_currency: Annotated[str, Query(min_length=3, max_length=3)] = "USD",
) -> DirectoryPageOut:
    """Employee directory (FR-1). Filters combine with AND; repeat a filter for OR
    within it (`?country=IN&country=US`). Paginate with the returned cursors."""
    try:
        page = list_employees(
            db,
            DirectoryQuery(
                q=q,
                departments=department or [],
                countries=country or [],
                job_titles=job_title or [],
                job_levels=job_level or [],
                statuses=list(status_ or []),
                sort=sort,
                order=order,
                cursor=cursor,
                limit=limit,
                reporting_currency=reporting_currency,
            ),
        )
    except CursorError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return DirectoryPageOut(
        items=[
            DirectoryItem(
                **EmployeeOut.model_validate(row.employee).model_dump(
                    include=set(DirectoryItem.model_fields)
                ),
                total_compensation=row.total,
                total_compensation_reporting=row.total_reporting,
            )
            for row in page.rows
        ],
        next_cursor=page.next_cursor,
        prev_cursor=page.prev_cursor,
        reporting_currency=page.reporting_currency,
        rates_as_of=page.rates_as_of,
    )


@router.get("/employees/{employee_id}", response_model=ProfileOut)
def get_profile(
    employee_id: int,
    db: DbSession,
    reporting_currency: Annotated[str, Query(min_length=3, max_length=3)] = "USD",
) -> ProfileOut:
    """Employee details, current compensation breakdown and full history (FR-2)."""
    profile = employee_service.get_profile(db, employee_id, reporting_currency)
    return ProfileOut(
        employee=EmployeeOut.model_validate(profile.employee),
        reporting_currency=profile.reporting_currency,
        rates_as_of=profile.rates_as_of,
        total_compensation=profile.total_compensation,
        total_compensation_reporting=profile.total_compensation_reporting,
        current=[
            CurrentCompensationOut(
                compensation_type_id=item.compensation_type.id,
                name=item.compensation_type.name,
                category=item.compensation_type.category,
                subtype=item.compensation_type.subtype,
                period_months=item.compensation_type.period_months,
                is_base_pay=item.compensation_type.is_base_pay,
                counts_toward_total=item.compensation_type.counts_toward_total,
                record_id=item.record_id,
                effective_date=item.effective_date,
                amount=item.amount,
                annual_amount=item.annual_amount,
                annual_amount_reporting=item.annual_amount_reporting,
            )
            for item in profile.current
        ],
        history=[
            HistoryItemOut(
                record_id=item.record.id,
                compensation_type_id=item.compensation_type.id,
                compensation_type=item.compensation_type.name,
                effective_date=item.record.effective_date,
                country=item.record.country,
                currency=item.record.currency,
                amount=item.record.amount,
                previous_amount=item.previous_amount,
                percent_change=item.percent_change,
                currency_changed=item.currency_changed,
                change_reason=item.reason.code,
                change_reason_label=item.reason.label,
                note=item.record.note,
                changed_by=item.record.changed_by,
                created_at=item.record.created_at,
            )
            for item in profile.history
        ],
    )


@router.patch("/employees/{employee_id}", response_model=EmployeeOut)
def update_employee(employee_id: int, body: EmployeeUpdate, db: DbSession) -> Employee:
    """Edit employee details. Compensation can't be changed here (FR-3)."""
    with translate_db_errors(db):
        employee = employee_service.update_employee(
            db, employee_id, body.model_dump(exclude_unset=True)
        )
        db.commit()
    db.refresh(employee)
    return employee


@router.post("/employees/{employee_id}/terminate", response_model=EmployeeOut)
def terminate_employee(employee_id: int, body: TerminateIn, db: DbSession) -> Employee:
    """Mark an employee terminated as of a date. They stay searchable; nothing is deleted."""
    with translate_db_errors(db):
        employee = employee_service.terminate_employee(
            db, employee_id, body.termination_date
        )
        db.commit()
    db.refresh(employee)
    return employee


@router.post(
    "/employees/{employee_id}/compensation-records",
    response_model=CompensationRecordOut,
    status_code=status.HTTP_201_CREATED,
)
def create_compensation_record(
    employee_id: int, body: CompensationRecordIn, db: DbSession
) -> CompensationRecord:
    """Append a record in the employee's current country and currency.

    Records are append-only: there is deliberately no update or delete endpoint.
    Current compensation follows records in effect today; Phase 7 adds the full
    compensation-change workflow (future dates, validation before the DB).
    """
    employee = get_or_404(db, Employee, employee_id, "employee")
    with translate_db_errors(db):
        record = append_record(db, employee, **body.model_dump())
        db.commit()
    db.refresh(record)
    return record


@router.get(
    "/employees/{employee_id}/compensation-records",
    response_model=list[CompensationRecordOut],
)
def list_compensation_records(
    employee_id: int, db: DbSession
) -> list[CompensationRecord]:
    """The employee's full history, newest first."""
    get_or_404(db, Employee, employee_id, "employee")
    return list(
        db.scalars(
            select(CompensationRecord)
            .where(CompensationRecord.employee_id == employee_id)
            .order_by(
                CompensationRecord.effective_date.desc(), CompensationRecord.id.desc()
            )
        )
    )


@router.get("/compensation-records/{record_id}", response_model=CompensationRecordOut)
def get_compensation_record(record_id: int, db: DbSession) -> CompensationRecord:
    return get_or_404(db, CompensationRecord, record_id, "compensation record")
