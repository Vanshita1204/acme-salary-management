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
from sqlalchemy.orm import Session

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
from app.models import CompensationType, CurrentCompensation, Employee, ExchangeRate
from app.services.exchange_rates import latest_rates

Sort = Literal["name", "hire_date", "compensation"]
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
NO_TOTAL = -1  # sort key for employees with no compensation yet: last when descending


@dataclass(frozen=True)
class DirectoryQuery:
    q: str | None = None
    departments: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    job_titles: list[str] = field(default_factory=list)
    job_levels: list[str] = field(default_factory=list)
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
        (Employee.department, query.departments),
        (Employee.current_country, [c.upper() for c in query.countries]),
        (Employee.job_title, query.job_titles),
        (Employee.job_level, query.job_levels),
        (Employee.status, query.statuses),
    ):
        if values:
            stmt = stmt.where(column.in_(values))
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


def list_employees(session: Session, query: DirectoryQuery) -> DirectoryPage:
    limit = max(1, min(query.limit, MAX_LIMIT))
    cursor = (
        decode_cursor(query.cursor, query.sort, query.order) if query.cursor else None
    )
    direction = cursor.direction if cursor else None
    ascending = reads_ascending(query.order, direction)

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
    totals = dict(
        session.execute(
            annual_totals().where(
                CurrentCompensation.employee_id.in_([e.id for e in employees])
            )
        ).all()
    )
    rates, rate_dates = latest_rates(session)
    reporting = query.reporting_currency.upper()

    def in_reporting(employee: Employee) -> Decimal | None:
        total = totals.get(employee.id)
        if total is None:
            return None
        try:
            return convert(total, employee.currency, reporting, rates)
        except MissingRateError:
            return None

    used = {e.currency for e in employees} | {reporting}
    return DirectoryPage(
        rows=[
            DirectoryRow(
                e,
                None
                if totals.get(e.id) is None
                else totals[e.id].quantize(Decimal("0.01")),
                in_reporting(e),
            )
            for e in employees
        ],
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        reporting_currency=reporting,
        rates_as_of={c: rate_dates[c] for c in sorted(used) if c in rate_dates},
    )
