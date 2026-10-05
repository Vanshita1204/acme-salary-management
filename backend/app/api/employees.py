from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.db_errors import translate_db_errors
from app.api.deps import DbSession, get_or_404
from app.models import CompensationRecord, Employee
from app.schemas.employees import (
    CompensationRecordIn,
    CompensationRecordOut,
    EmployeeIn,
    EmployeeOut,
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
