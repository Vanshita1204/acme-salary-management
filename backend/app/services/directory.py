"""Employee directory (FR-1): search, filters, sort, keyset pagination, totals.

Totals come from `current_compensation` (Scale Lab E3) — annualized, counting only
types with `counts_toward_total` — so nothing is recomputed from history per request.
Sorting by compensation compares totals in USD at the latest stored rates; that's the
same ordering as any reporting currency, since converting divides every total by the
same rate.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import ColumnElement, Select, String, cast, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, aliased

from app.domain.currency import MissingRateError, convert
from app.domain.pagination import (
    INVALID_CURSOR,
    Cursor,
    CursorError,
    Order,
    decode_cursor,
    encode_cursor,
    reads_ascending,
)
from app.models import (
    CompensationType,
    CurrentCompensation,
    Employee,
    ExchangeRate,
    JobLevel,
)
from app.services.compensation import promote_due_records
from app.services.exchange_rates import latest_rates, require_supported_currency

Sort = Literal["name", "hire_date", "level", "compensation"]
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
CENT = Decimal("0.01")
EXPORT_BATCH = 1_000  # employees per totals query when exporting
NO_TOTAL = -1  # sort key for employees with no compensation yet: last when descending


@dataclass(frozen=True)
class DirectoryQuery:
    q: str | None = None
    department_ids: list[int] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    job_title_ids: list[int] = field(default_factory=list)
    job_level_ids: list[int] = field(default_factory=list)
    min_level_rank: int | None = None  # inclusive, by job_levels.rank
    max_level_rank: int | None = None
    statuses: list[str] = field(default_factory=list)
    sort: Sort = "name"
    order: Order = "asc"
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    reporting_currency: str = "USD"


@dataclass(frozen=True)
class DirectoryRow:
    employee: Employee
    total: Decimal | None  # annual, employee's currency
    total_reporting: (
        Decimal | None
    )  # annual, reporting currency; None if a rate is missing


@dataclass(frozen=True)
class DirectoryPage:
    rows: list[DirectoryRow]
    next_cursor: str | None
    prev_cursor: str | None
    reporting_currency: str
    rates_as_of: dict[str, date]


def annual_totals() -> Select:
    """employee_id -> annual total compensation (CTC) in the employee's currency."""
    return (
        select(
            CurrentCompensation.employee_id,
            func.sum(
                CurrentCompensation.amount * 12 / CompensationType.period_months
            ).label("total"),
        )
        .join(
            CompensationType,
            CompensationType.id == CurrentCompensation.compensation_type_id,
        )
        .where(CompensationType.counts_toward_total)
        .group_by(CurrentCompensation.employee_id)
    )


def latest_rate_rows() -> Select:
    return (
        select(ExchangeRate.currency, ExchangeRate.rate_to_usd)
        .ext(distinct_on(ExchangeRate.currency))
        .order_by(ExchangeRate.currency, ExchangeRate.rate_date.desc())
    )


def escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def apply_filters(stmt: Select, query: DirectoryQuery) -> Select:
    if query.q and query.q.strip():
        pattern = f"%{escape_like(query.q.strip())}%"
        full_name = Employee.first_name + " " + Employee.last_name
        stmt = stmt.where(
            or_(
                full_name.ilike(pattern, escape="\\"),
                Employee.last_name.ilike(pattern, escape="\\"),
                cast(Employee.email, String).ilike(pattern, escape="\\"),
                Employee.code.ilike(pattern, escape="\\"),
            )
        )
    for column, values in (
        (Employee.department_id, query.department_ids),
        (Employee.current_country, [c.upper() for c in query.countries]),
        (Employee.job_title_id, query.job_title_ids),
        (Employee.job_level_id, query.job_level_ids),
        (Employee.status, query.statuses),
    ):
        if values:
            stmt = stmt.where(column.in_(values))
    if query.min_level_rank is not None or query.max_level_rank is not None:
        levels = select(JobLevel.id)
        if query.min_level_rank is not None:
            levels = levels.where(JobLevel.rank >= query.min_level_rank)
        if query.max_level_rank is not None:
            levels = levels.where(JobLevel.rank <= query.max_level_rank)
        stmt = stmt.where(Employee.job_level_id.in_(levels))
    return stmt


def sort_key(query: DirectoryQuery) -> tuple[Select, list[ColumnElement], list]:
    """The base statement, its sort-key expressions, and how to parse cursor values."""
    stmt = select(Employee)
    if query.sort == "name":
        keys = [
            func.lower(Employee.last_name),
            func.lower(Employee.first_name),
            Employee.id,
        ]
        parsers = [str, str, int]
    elif query.sort == "hire_date":
        keys = [Employee.hire_date, Employee.id]
        parsers = [date.fromisoformat, int]
    elif query.sort == "level":
        # By seniority (job_levels.rank, so L2 before L10), then name.
        level = aliased(JobLevel)
        stmt = stmt.join(level, level.id == Employee.job_level_id)
        keys = [
            level.rank,
            func.lower(Employee.last_name),
            func.lower(Employee.first_name),
            Employee.id,
        ]
        parsers = [int, str, str, int]
    else:
        totals = annual_totals().subquery()
        rates = latest_rate_rows().subquery()
        stmt = stmt.outerjoin(totals, totals.c.employee_id == Employee.id).outerjoin(
            rates, rates.c.currency == Employee.currency
        )
        total_usd = func.coalesce(totals.c.total * rates.c.rate_to_usd, NO_TOTAL)
        keys = [total_usd, Employee.id]
        parsers = [Decimal, int]
    return stmt, keys, parsers


def with_totals(
    session: Session,
    employees: list[Employee],
    reporting: str,
    rates: dict[str, Decimal],
) -> list[DirectoryRow]:
    """Each employee with their annual total in their own and the reporting currency."""
    totals = dict(
        session.execute(
            annual_totals().where(
                CurrentCompensation.employee_id.in_([e.id for e in employees])
            )
        ).all()
    )

    def in_reporting(employee: Employee) -> Decimal | None:
        total = totals.get(employee.id)
        if total is None:
            return None
        try:
            return convert(total, employee.currency, reporting, rates)
        except MissingRateError:
            return None

    return [
        DirectoryRow(
            e,
            None if totals.get(e.id) is None else totals[e.id].quantize(CENT),
            in_reporting(e),
        )
        for e in employees
    ]


def list_employees(session: Session, query: DirectoryQuery) -> DirectoryPage:
    limit = max(1, min(query.limit, MAX_LIMIT))
    cursor = (
        decode_cursor(query.cursor, query.sort, query.order) if query.cursor else None
    )
    direction = cursor.direction if cursor else None
    ascending = reads_ascending(query.order, direction)

    reporting = require_supported_currency(session, query.reporting_currency)
    promote_due_records(session)  # future-dated records whose date has arrived
    stmt, keys, parsers = sort_key(query)
    stmt = apply_filters(stmt, query)
    if cursor:
        try:
            values = [parse(v) for parse, v in zip(parsers, cursor.key, strict=True)]
        except (ValueError, TypeError, ArithmeticError) as exc:  # tampered key values
            raise CursorError(INVALID_CURSOR) from exc
        boundary = (
            tuple_(*keys) > tuple_(*values)
            if ascending
            else tuple_(*keys) < tuple_(*values)
        )
        stmt = stmt.where(boundary)
    stmt = stmt.add_columns(*[k.label(f"k{i}") for i, k in enumerate(keys)])
    stmt = stmt.order_by(*[k.asc() if ascending else k.desc() for k in keys]).limit(
        limit + 1
    )

    fetched = session.execute(stmt).all()
    has_more = len(fetched) > limit
    fetched = fetched[:limit]
    if direction == "before":
        fetched.reverse()  # read backwards; present in the requested order

    def boundary_cursor(row, way: str) -> str:
        key = tuple(
            None if row[i + 1] is None else str(row[i + 1]) for i in range(len(keys))
        )
        return encode_cursor(Cursor(query.sort, query.order, way, key))

    # Going forward, a previous page exists iff we arrived via a cursor; going back,
    # a next page always exists (we came from it), and a previous one iff has_more.
    if direction == "before":
        next_cursor = boundary_cursor(fetched[-1], "after") if fetched else None
        prev_cursor = (
            boundary_cursor(fetched[0], "before") if fetched and has_more else None
        )
    else:
        next_cursor = (
            boundary_cursor(fetched[-1], "after") if fetched and has_more else None
        )
        prev_cursor = (
            boundary_cursor(fetched[0], "before") if fetched and cursor else None
        )

    employees = [row[0] for row in fetched]
    rates, rate_dates = latest_rates(session)
    used = {e.currency for e in employees} | {reporting}
    return DirectoryPage(
        rows=with_totals(session, employees, reporting, rates),
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        reporting_currency=reporting,
        rates_as_of={c: rate_dates[c] for c in sorted(used) if c in rate_dates},
    )


@dataclass(frozen=True)
class DirectoryExport:
    rows: list[DirectoryRow]  # every match, in the directory's order
    reporting_currency: str
    rate_dates: dict[str, date]  # every stored rate's date, by currency


def export_employees(session: Session, query: DirectoryQuery) -> DirectoryExport:
    """Every employee matching the directory's search and filters, in its sort order,
    with the same totals (FR-6). The same statement as `list_employees`, unpaged, so
    the export is exactly what paging through the directory shows."""
    reporting = require_supported_currency(session, query.reporting_currency)
    promote_due_records(session)
    stmt, keys, _ = sort_key(query)
    stmt = apply_filters(stmt, query)
    ascending = query.order == "asc"
    stmt = stmt.order_by(*[k.asc() if ascending else k.desc() for k in keys])
    employees = list(session.scalars(stmt).unique())

    rates, rate_dates = latest_rates(session)
    rows = []
    for start in range(0, len(employees), EXPORT_BATCH):
        batch = employees[start : start + EXPORT_BATCH]
        rows += with_totals(session, batch, reporting, rates)
    return DirectoryExport(
        rows=rows, reporting_currency=reporting, rate_dates=rate_dates
    )
