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
    DirectoryItem,
    DirectoryPageOut,
    EmployeeIn,
    EmployeeOut,
    Status,
)
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
    """Basic create. Phase 6 adds the required initial base-pay record (FR-2)."""
    employee = Employee(**body.model_dump())
    with translate_db_errors(db):
        db.add(employee)
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


@router.get("/employees/{employee_id}", response_model=EmployeeOut)
def get_employee(employee_id: int, db: DbSession) -> Employee:
    return get_or_404(db, Employee, employee_id, "employee")


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
    Phase 7 adds keeping `current_compensation` in step.
    """
    employee = get_or_404(db, Employee, employee_id, "employee")
    record = CompensationRecord(
        **body.model_dump(),
        employee_id=employee.id,
        country=employee.current_country,
        currency=employee.currency,
    )
    with translate_db_errors(db):
        db.add(record)
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
