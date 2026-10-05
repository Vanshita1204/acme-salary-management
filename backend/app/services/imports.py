"""CSV employee import (FR-5): parse, validate against the database, and insert.

Validation itself is the pure `app.domain.csv_import`; this module loads what it needs
from the database and writes the result. Confirm re-validates against the current
database rather than trusting an earlier preview, so an email created since the preview
is still caught. The insert is all-or-nothing: one transaction, committed by the caller.
"""

import csv
import io
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.domain.csv_import import (
    TEMPLATE_COLUMNS,
    ImportContext,
    ImportResult,
    ImportRow,
    validate_import,
)
from app.models import (
    Company,
    CompensationRecord,
    Country,
    Currency,
    CurrentCompensation,
    Department,
    Employee,
    JobLevel,
    JobTitle,
)
from app.services.compensation import reason_id, utc_today
from app.services.employees import NEW_HIRE, base_pay_type_id
from app.services.errors import Problem, ServiceError

# 10,000 rows of the template run to ~2 MB; this leaves room for long names.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

NOT_UTF8 = "file must be a UTF-8 encoded CSV"
NOT_CSV = "file could not be read as CSV: {error}"
TOO_LARGE = "file is larger than {limit} MB"


@dataclass(frozen=True)
class ParsedFile:
    header: list[str]
    records: list[dict[str, str]]


def template_csv() -> str:
    """The header row HR fills in: one row per employee, base pay only."""
    return ",".join(TEMPLATE_COLUMNS) + "\r\n"


def parse_csv(content: bytes) -> ParsedFile:
    """Decode and split a CSV upload. Header names are trimmed and lower-cased.

    The upload size limit bounds the work; the MAX_ROWS cap is then checked by
    validation before any row is validated.
    """
    if len(content) > MAX_UPLOAD_BYTES:
        raise ServiceError(
            Problem.TOO_LARGE, TOO_LARGE.format(limit=MAX_UPLOAD_BYTES // 2**20)
        )
    try:
        text = content.decode("utf-8-sig")  # Excel adds a byte-order mark
    except UnicodeDecodeError as exc:
        raise ServiceError(Problem.INVALID, NOT_UTF8) from exc
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        header = [name.strip().lower() for name in next(reader, [])]
        records = []
        for values in reader:
            if not any(v.strip() for v in values):
                continue  # blank line, e.g. trailing ones Excel leaves
            records.append(dict(zip(header, values, strict=False)))
    except csv.Error as exc:
        raise ServiceError(Problem.INVALID, NOT_CSV.format(error=exc)) from exc
    return ParsedFile(header=header, records=records)


def load_context(session: Session, records: list[dict[str, str]]) -> ImportContext:
    """Everything validation needs, read once. Only this file's emails are checked
    for existing employees, so the lookup doesn't grow with the employee table."""
    emails = {(r.get("email") or "").strip() for r in records} - {""}
    existing = (
        session.scalars(
            select(Employee.email).where(
                func.lower(Employee.email).in_([e.lower() for e in emails])
            )
        )
        if emails
        else []
    )
    return ImportContext.build(
        companies=dict(session.execute(select(Company.name, Company.id)).all()),
        countries=session.scalars(select(Country.code)),
        currencies=session.scalars(select(Currency.code)),
        existing_emails=existing,
        departments=dict(session.execute(select(Department.name, Department.id)).all()),
        job_titles=dict(session.execute(select(JobTitle.name, JobTitle.id)).all()),
        job_levels=dict(session.execute(select(JobLevel.code, JobLevel.id)).all()),
    )


def validate_file(session: Session, content: bytes) -> ImportResult:
    parsed = parse_csv(content)
    return validate_import(
        parsed.header, parsed.records, load_context(session, parsed.records)
    )


def insert_rows(
    session: Session, rows: list[ImportRow], changed_by: str, today: date | None = None
) -> list[int]:
    """Insert validated rows: employees, their "new hire" base-pay records, and the
    current-compensation pointers. Set-based, so 10,000 rows is three statements.

    A hire date in the future gives a future-dated record, which becomes current on
    that date like any other (Phase 7)."""
    if not rows:
        return []
    today = today or utc_today()
    employee_ids = list(
        session.scalars(
            insert(Employee).returning(Employee.id, sort_by_parameter_order=True),
            [
                {
                    "company_id": row.company_id,
                    "first_name": row.first_name,
                    "last_name": row.last_name,
                    "email": row.email,
                    "department_id": row.department_id,
                    "job_title_id": row.job_title_id,
                    "job_level_id": row.job_level_id,
                    "current_country": row.country,
                    "currency": row.currency,
                    "status": "active",
                    "hire_date": row.hire_date,
                }
                for row in rows
            ],
        )
    )
    base_pay, new_hire = base_pay_type_id(session), reason_id(session, NEW_HIRE)
    # The DB triggers still check every record (hire date, currency, base pay > 0).
    record_ids = list(
        session.scalars(
            insert(CompensationRecord).returning(
                CompensationRecord.id, sort_by_parameter_order=True
            ),
            [
                {
                    "employee_id": employee_id,
                    "compensation_type_id": base_pay,
                    "change_reason_id": new_hire,
                    "effective_date": row.hire_date,
                    "amount": row.base_pay_amount,
                    "country": row.country,
                    "currency": row.currency,
                    "changed_by": changed_by,
                }
                for employee_id, row in zip(employee_ids, rows, strict=True)
            ],
        )
    )
    current = [
        {
            "employee_id": employee_id,
            "compensation_type_id": base_pay,
            "compensation_record_id": record_id,
            "effective_date": row.hire_date,
            "amount": row.base_pay_amount,
        }
        for employee_id, record_id, row in zip(
            employee_ids, record_ids, rows, strict=True
        )
        if row.hire_date <= today
    ]
    if current:
        session.execute(insert(CurrentCompensation), current)
    return employee_ids


def confirm_file(
    session: Session, content: bytes, changed_by: str, today: date | None = None
) -> tuple[ImportResult, list[int]]:
    """Re-validate, then insert everything or nothing. Nothing is written when the
    result has errors; the caller commits on success."""
    result = validate_file(session, content)
    if not result.ok:
        return result, []
    return result, insert_rows(session, result.rows, changed_by, today)
