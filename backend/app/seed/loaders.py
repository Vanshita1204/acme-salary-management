"""Ways of writing seed rows to Postgres — compared in Scale Lab experiment E1.

Every loader takes plain dicts and returns, for employees, the new ids in input order.
"""

from collections.abc import Callable, Sequence
from typing import Literal

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.models import CompensationRecord, Employee

Method = Literal["row", "orm", "batch", "copy"]
METHODS: tuple[Method, ...] = ("row", "orm", "batch", "copy")

EMPLOYEE_COLUMNS = (
    "company_id",
    "first_name",
    "last_name",
    "email",
    "department_id",
    "job_title_id",
    "job_level_id",
    "current_country",
    "currency",
    "status",
    "hire_date",
    "termination_date",
)
RECORD_COLUMNS = (
    "employee_id",
    "compensation_type_id",
    "effective_date",
    "country",
    "currency",
    "amount",
    "change_reason_id",
    "changed_by",
)


# --- row: one INSERT statement and round trip per row ---


def employees_row(session: Session, rows: Sequence[dict]) -> list[int]:
    stmt = insert(Employee).returning(Employee.id)
    return [session.scalar(stmt, row) for row in rows]


def records_row(session: Session, rows: Sequence[dict]) -> None:
    stmt = insert(CompensationRecord)
    for row in rows:
        session.execute(stmt, row)


# --- orm: mapped objects through the unit of work ---


def employees_orm(session: Session, rows: Sequence[dict]) -> list[int]:
    objects = [Employee(**row) for row in rows]
    session.add_all(objects)
    session.flush()
    return [obj.id for obj in objects]


def records_orm(session: Session, rows: Sequence[dict]) -> None:
    session.add_all(CompensationRecord(**row) for row in rows)
    session.flush()


# --- batch: multi-row INSERTs (SQLAlchemy "insertmanyvalues") ---


def employees_batch(session: Session, rows: Sequence[dict]) -> list[int]:
    stmt = insert(Employee).returning(Employee.id, sort_by_parameter_order=True)
    return list(session.scalars(stmt, list(rows)))


def records_batch(session: Session, rows: Sequence[dict]) -> None:
    session.execute(insert(CompensationRecord), list(rows))


# --- copy: Postgres COPY FROM STDIN ---


def _copy(
    session: Session, table: str, columns: Sequence[str], rows: Sequence[dict]
) -> None:
    driver = session.connection().connection.driver_connection  # psycopg connection
    with (
        driver.cursor() as cursor,
        cursor.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy,
    ):
        for row in rows:
            copy.write_row([row[c] for c in columns])


def employees_copy(session: Session, rows: Sequence[dict]) -> list[int]:
    _copy(session, "employees", EMPLOYEE_COLUMNS, rows)
    # COPY can't return generated ids; emails are unique, so look them up.
    emails = [row["email"] for row in rows]
    ids = dict(
        session.execute(
            select(Employee.email, Employee.id).where(Employee.email.in_(emails))
        ).all()
    )
    return [ids[email] for email in emails]


def records_copy(session: Session, rows: Sequence[dict]) -> None:
    _copy(session, "compensation_records", RECORD_COLUMNS, rows)


EMPLOYEE_LOADERS: dict[Method, Callable[[Session, Sequence[dict]], list[int]]] = {
    "row": employees_row,
    "orm": employees_orm,
    "batch": employees_batch,
    "copy": employees_copy,
}
RECORD_LOADERS: dict[Method, Callable[[Session, Sequence[dict]], None]] = {
    "row": records_row,
    "orm": records_orm,
    "batch": records_batch,
    "copy": records_copy,
}
