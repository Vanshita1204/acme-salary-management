from dataclasses import replace
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select

from app.api.db_errors import translate_db_errors
from app.api.deps import DbSession, get_or_404
from app.domain.pagination import CursorError
from app.models import CompensationRecord, Employee
from app.schemas.employees import (
    CompensationChangeOut,
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
from app.services.compensation import record_change, utc_today
from app.services.directory import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    DirectoryQuery,
    export_employees,
    list_employees,
)
from app.services.exports import directory_csv

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


def directory_query(
    q: Annotated[
        str | None, Query(description="Search name, email or employee code")
    ] = None,
    department_id: Annotated[list[int] | None, Query()] = None,
    country: Annotated[list[str] | None, Query()] = None,
    job_title_id: Annotated[list[int] | None, Query()] = None,
    job_level_id: Annotated[list[int] | None, Query()] = None,
    min_level_rank: Annotated[
        int | None, Query(description="Only levels at or above this rank")
    ] = None,
    max_level_rank: Annotated[
        int | None, Query(description="Only levels at or below this rank")
    ] = None,
    status_: Annotated[list[Status] | None, Query(alias="status")] = None,
    sort: Literal["name", "hire_date", "level", "compensation"] = "name",
    order: Literal["asc", "desc"] = "asc",
    reporting_currency: Annotated[str, Query(min_length=3, max_length=3)] = "USD",
) -> DirectoryQuery:
    """The directory's search, filters and sort — shared by the page and the export,
    so an export is always of exactly the view on screen."""
    return DirectoryQuery(
        q=q,
        department_ids=department_id or [],
        countries=country or [],
        job_title_ids=job_title_id or [],
        job_level_ids=job_level_id or [],
        min_level_rank=min_level_rank,
        max_level_rank=max_level_rank,
        statuses=list(status_ or []),
        sort=sort,
        order=order,
        reporting_currency=reporting_currency,
    )


DirectoryView = Annotated[DirectoryQuery, Depends(directory_query)]


@router.get("/employees", response_model=DirectoryPageOut)
def list_directory(
    db: DbSession,
    view: DirectoryView,
    cursor: Annotated[
        str | None, Query(description="next_cursor or prev_cursor")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> DirectoryPageOut:
    """Employee directory (FR-1). Filters combine with AND; repeat a filter for OR
    within it (`?country=IN&country=US`, `?department_id=1&department_id=4`).
    Department, title and level filter by id (`GET /departments`, `/job-titles`,
    `/job-levels`). Paginate with the returned cursors."""
    try:
        page = list_employees(db, replace(view, cursor=cursor, limit=limit))
    except CursorError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    out = DirectoryPageOut(
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
    db.commit()  # keep future-dated records promoted while reading (after building: commit expires)
    return out


@router.get(
    "/employees/export",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
def export_directory(db: DbSession, view: DirectoryView) -> Response:
    """The directory as CSV (FR-6): every employee matching the same search, filters
    and sort as `GET /employees`, unpaged, with annual total compensation in their
    own and the reporting currency."""
    content = directory_csv(export_employees(db, view))
    db.commit()  # keep future-dated records promoted while reading
    filename = f"employees_{utc_today().isoformat()}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/employees/{employee_id}", response_model=ProfileOut)
def get_profile(
    employee_id: int,
    db: DbSession,
    reporting_currency: Annotated[str, Query(min_length=3, max_length=3)] = "USD",
) -> ProfileOut:
    """Employee details, current compensation breakdown and full history (FR-2)."""
    profile = employee_service.get_profile(db, employee_id, reporting_currency)
    out = ProfileOut(
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
    db.commit()  # keep future-dated records promoted while reading (after building: commit expires)
    return out


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
    "/employees/{employee_id}/compensation",
    response_model=CompensationChangeOut,
    status_code=status.HTTP_201_CREATED,
)
def record_compensation_change(
    employee_id: int, body: CompensationRecordIn, db: DbSession
) -> CompensationChangeOut:
    """Record a compensation change for one type (FR-4).

    Appends a record in the employee's current country and currency; history is
    append-only, so mistakes are fixed with a new "correction" record. A future-dated
    change is stored now and becomes current on its effective date.
    """
    with translate_db_errors(db):
        change = record_change(db, employee_id, **body.model_dump())
        db.commit()
    db.refresh(change.record)
    return CompensationChangeOut(
        **CompensationRecordOut.model_validate(change.record).model_dump(),
        is_current=change.is_current,
    )


# The Phase 1.3 path, kept for existing clients: same operation, same rules.
router.add_api_route(
    "/employees/{employee_id}/compensation-records",
    record_compensation_change,
    methods=["POST"],
    response_model=CompensationChangeOut,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)


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
