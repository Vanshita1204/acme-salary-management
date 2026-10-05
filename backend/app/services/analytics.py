"""Pay insights (§5), computed in SQL.

Every view starts from the same population: **active** employees matching the
directory's search and filters (a `status` filter is ignored — analytics are always
active-only), each with their annual total compensation (CTC) from current
compensation, converted to the reporting currency through the latest stored USD rates.
Employees whose currency has no stored rate can't be converted; they're left out and
counted in `excluded_no_rate` rather than silently dropped.

`app.domain.analytics` holds the reference definitions (peer groups, outlier
thresholds, composition) these queries are tested against.
"""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    ColumnElement,
    Select,
    Text,
    and_,
    case,
    cast,
    func,
    literal,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, array, array_agg
from sqlalchemy.orm import Session

from app.domain.analytics import MIN_PEER_GROUP_SIZE, OUTLIER_HIGH, OUTLIER_LOW
from app.domain.compensation import NON_INCREASE_REASONS
from app.models import (
    ChangeReason,
    CompensationRecord,
    CompensationType,
    Country,
    CurrentCompensation,
    Department,
    Employee,
    JobLevel,
    JobTitle,
)
from app.services.compensation import promote_due_records, utc_today
from app.services.directory import (
    DirectoryQuery,
    annual_totals,
    apply_filters,
    latest_rate_rows,
)
from app.services.errors import Problem, ServiceError
from app.services.exchange_rates import latest_rates, require_supported_currency
from app.services.relocation import RELOCATION

CENT = Decimal("0.01")
ACTIVE = "active"
MAX_BINS = 100
INVALID_RANGE = "date_from must not be after date_to"

GROUPS = {  # group_by value -> (label column, sort column)
    "department": (Department.name, Department.name),
    "country": (Country.name, Country.name),
    "title": (JobTitle.name, JobTitle.name),
    "level": (JobLevel.code, JobLevel.rank),
}


@dataclass(frozen=True)
class Context:
    """Where a view's numbers come from, returned with every view (FR-8)."""

    reporting_currency: str
    rates_as_of: dict[str, date]  # every rate used, by currency
    excluded_no_rate: int  # active matching employees whose currency has no rate


def money(value) -> Decimal | None:
    return None if value is None else Decimal(value).quantize(CENT)


def population(session: Session, query: DirectoryQuery, reporting: str) -> Select:
    """One row per active, matching employee with current compensation:
    ids for grouping, `total_local` (employee's currency) and `total` (reporting
    currency, NULL when a rate is missing)."""
    totals = annual_totals().subquery()
    rates = latest_rate_rows().subquery()
    reporting_rate = (
        select(rates.c.rate_to_usd)
        .where(rates.c.currency == reporting)
        .scalar_subquery()
    )
    stmt = (
        select(
            Employee.id.label("employee_id"),
            Employee.department_id,
            Employee.job_title_id,
            Employee.job_level_id,
            Employee.current_country.label("country"),
            Employee.currency,
            totals.c.total.label("total_local"),
            (totals.c.total * rates.c.rate_to_usd / reporting_rate).label("total"),
        )
        .join(totals, totals.c.employee_id == Employee.id)
        .outerjoin(rates, rates.c.currency == Employee.currency)
        .where(Employee.status == ACTIVE)
    )
    return apply_filters(stmt, replace(query, statuses=[]))


def prepare(session: Session, query: DirectoryQuery) -> tuple[str, Select, Context]:
    reporting = require_supported_currency(session, query.reporting_currency)
    promote_due_records(session)
    pop = population(session, query, reporting).subquery()
    currencies, excluded = session.execute(
        select(
            func.array_agg(pop.c.currency.distinct()).filter(pop.c.total.is_not(None)),
            func.count().filter(pop.c.total.is_(None)),
        )
    ).one()
    _, rate_dates = latest_rates(session)
    used = set(currencies or []) | {reporting}
    context = Context(
        reporting_currency=reporting,
        rates_as_of={c: rate_dates[c] for c in sorted(used) if c in rate_dates},
        excluded_no_rate=excluded,
    )
    return reporting, pop, context


# --- summary cards and cost breakdown ---


@dataclass(frozen=True)
class CostRow:
    key: str
    label: str
    headcount: int
    total_cost: Decimal
    share: Decimal  # of the overall cost


@dataclass(frozen=True)
class Summary:
    context: Context
    headcount: int
    total_cost: Decimal | None
    average: Decimal | None
    median: Decimal | None
    by_country: list[CostRow]
    by_department: list[CostRow]


def summary(session: Session, query: DirectoryQuery) -> Summary:
    _, pop, context = prepare(session, query)
    priced = select(pop).where(pop.c.total.is_not(None)).subquery()
    headcount, total, average, median = session.execute(
        select(
            func.count(),
            func.sum(priced.c.total),
            func.avg(priced.c.total),
            func.percentile_cont(0.5).within_group(priced.c.total),
        )
    ).one()

    def breakdown(key_col, label_col, join_model, join_on) -> list[CostRow]:
        rows = session.execute(
            select(key_col, label_col, func.count(), func.sum(priced.c.total))
            .select_from(priced)
            .join(join_model, join_on)
            .group_by(key_col, label_col)
            .order_by(func.sum(priced.c.total).desc(), label_col)
        ).all()
        return [
            CostRow(
                key=str(key),
                label=label,
                headcount=count,
                total_cost=money(cost),
                share=(Decimal(cost) / Decimal(total)).quantize(Decimal("0.0001")),
            )
            for key, label, count, cost in rows
        ]

    return Summary(
        context=context,
        headcount=headcount,
        total_cost=money(total),
        average=money(average),
        median=money(median),
        by_country=breakdown(
            Country.code, Country.name, Country, Country.code == priced.c.country
        ),
        by_department=breakdown(
            Department.id,
            Department.name,
            Department,
            Department.id == priced.c.department_id,
        ),
    )


# --- grouped statistics ---


@dataclass(frozen=True)
class StatsRow:
    key: str
    label: str
    headcount: int
    average: Decimal
    median: Decimal
    minimum: Decimal
    maximum: Decimal


def _group_join(group_by: str, pop):
    return {
        "department": (Department.id, Department, Department.id == pop.c.department_id),
        "country": (Country.code, Country, Country.code == pop.c.country),
        "title": (JobTitle.id, JobTitle, JobTitle.id == pop.c.job_title_id),
        "level": (JobLevel.id, JobLevel, JobLevel.id == pop.c.job_level_id),
    }[group_by]


def grouped_stats(
    session: Session, query: DirectoryQuery, group_by: str
) -> tuple[Context, list[StatsRow]]:
    """Average, median, minimum and maximum total pay per group. Levels are listed
    by rank (most junior first); everything else by name."""
    _, pop, context = prepare(session, query)
    key, model, on = _group_join(group_by, pop)
    label, order = GROUPS[group_by]
    total = pop.c.total
    rows = session.execute(
        select(
            key,
            label,
            func.count(),
            func.avg(total),
            func.percentile_cont(0.5).within_group(total),
            func.min(total),
            func.max(total),
        )
        .select_from(pop)
        .join(model, on)
        .where(total.is_not(None))
        .group_by(key, label, order)
        .order_by(order)
    ).all()
    return context, [
        StatsRow(str(k), lbl, n, money(avg), money(med), money(lo), money(hi))
        for k, lbl, n, avg, med, lo, hi in rows
    ]


# --- role-by-country comparison ---


@dataclass(frozen=True)
class RoleCountryRow:
    job_title_id: int
    job_title: str
    job_level_id: int
    job_level: str
    country: str
    headcount: int
    average: Decimal
    median: Decimal


def role_by_country(
    session: Session, query: DirectoryQuery
) -> tuple[Context, list[RoleCountryRow]]:
    """Pay for the same title and level side by side across countries. Narrow it with
    the directory's title/level filters (e.g. `job_title_id=…&job_level_id=…`)."""
    _, pop, context = prepare(session, query)
    total = pop.c.total
    rows = session.execute(
        select(
            JobTitle.id,
            JobTitle.name,
            JobLevel.id,
            JobLevel.code,
            pop.c.country,
            func.count(),
            func.avg(total),
            func.percentile_cont(0.5).within_group(total),
        )
        .select_from(pop)
        .join(JobTitle, JobTitle.id == pop.c.job_title_id)
        .join(JobLevel, JobLevel.id == pop.c.job_level_id)
        .where(total.is_not(None))
        .group_by(
            JobTitle.id,
            JobTitle.name,
            JobLevel.id,
            JobLevel.code,
            JobLevel.rank,
            pop.c.country,
        )
        .order_by(
            JobTitle.name,
            JobLevel.rank,
            func.percentile_cont(0.5).within_group(total).desc(),
        )
    ).all()
    return context, [
        RoleCountryRow(t, tn, lvl, lc, c, n, money(avg), money(med))
        for t, tn, lvl, lc, c, n, avg, med in rows
    ]


# --- histogram ---


@dataclass(frozen=True)
class Bin:
    lower: Decimal
    upper: Decimal
    count: int


def histogram(
    session: Session, query: DirectoryQuery, bins: int
) -> tuple[Context, list[Bin]]:
    """Equal-width bins from the lowest to the highest total. The last bin includes
    its upper bound."""
    _, pop, context = prepare(session, query)
    total = pop.c.total
    lo, hi = session.execute(
        select(func.min(total), func.max(total)).where(total.is_not(None))
    ).one()
    if lo is None:
        return context, []
    if lo == hi:
        count = session.scalar(select(func.count()).where(total.is_not(None)))
        return context, [Bin(money(lo), money(hi), count)]
    bucket = func.least(func.width_bucket(total, lo, hi, bins), bins)
    counts = dict(
        session.execute(
            select(bucket, func.count()).where(total.is_not(None)).group_by(bucket)
        ).all()
    )
    width = (Decimal(hi) - Decimal(lo)) / bins
    return context, [
        Bin(
            money(Decimal(lo) + width * i),
            money(Decimal(lo) + width * (i + 1)),
            counts.get(i + 1, 0),
        )
        for i in range(bins)
    ]


# --- outliers ---


@dataclass(frozen=True)
class OutlierRow:
    employee_id: int
    code: str
    first_name: str
    last_name: str
    job_title: str
    job_level: str
    country: str
    total: Decimal
    peer_median: Decimal
    peer_count: int
    ratio: Decimal
    direction: str  # "below" or "above"


def outliers(
    session: Session, query: DirectoryQuery
) -> tuple[Context, list[OutlierRow]]:
    """Employees paid below 80% or above 120% of their peer-group median (same title,
    level and country), in groups of at least 5 (§5). Furthest from the median first.

    Peer groups are formed from the filtered population, so filtering to one
    department compares people only with peers in that department."""
    _, pop, context = prepare(session, query)
    priced = select(pop).where(pop.c.total.is_not(None)).subquery()
    groups = (
        select(
            priced.c.job_title_id,
            priced.c.job_level_id,
            priced.c.country,
            func.count().label("peer_count"),
            func.percentile_cont(0.5).within_group(priced.c.total).label("peer_median"),
        )
        .group_by(priced.c.job_title_id, priced.c.job_level_id, priced.c.country)
        .having(func.count() >= MIN_PEER_GROUP_SIZE)
        .subquery()
    )
    ratio = priced.c.total / groups.c.peer_median
    flagged = (
        select(
            Employee.id,
            Employee.code,
            Employee.first_name,
            Employee.last_name,
            JobTitle.name,
            JobLevel.code,
            priced.c.country,
            priced.c.total,
            groups.c.peer_median,
            groups.c.peer_count,
            ratio.label("ratio"),
        )
        .select_from(priced)
        .join(
            groups,
            and_(
                groups.c.job_title_id == priced.c.job_title_id,
                groups.c.job_level_id == priced.c.job_level_id,
                groups.c.country == priced.c.country,
            ),
        )
        .join(Employee, Employee.id == priced.c.employee_id)
        .join(JobTitle, JobTitle.id == priced.c.job_title_id)
        .join(JobLevel, JobLevel.id == priced.c.job_level_id)
        .where(
            groups.c.peer_median > 0,
            (ratio < OUTLIER_LOW) | (ratio > OUTLIER_HIGH),
        )
        .order_by(func.abs(func.ln(ratio)).desc(), Employee.id)
    )
    return context, [
        OutlierRow(
            employee_id=eid,
            code=code,
            first_name=first,
            last_name=last,
            job_title=title,
            job_level=level,
            country=country,
            total=money(total),
            peer_median=money(peer_median),
            peer_count=peer_count,
            ratio=Decimal(r).quantize(Decimal("0.0001")),
            direction="below" if r < OUTLIER_LOW else "above",
        )
        for eid, code, first, last, title, level, country, total, peer_median, peer_count, r in session.execute(
            flagged
        ).all()
    ]


# --- composition ---


@dataclass(frozen=True)
class CompositionRow:
    category: str
    amount: Decimal  # annual, reporting currency
    share: Decimal


def composition(
    session: Session, query: DirectoryQuery
) -> tuple[Context, list[CompositionRow]]:
    """Share of total compensation (CTC) per compensation-type category. Types outside
    CTC are left out, so the shares add up to the same totals as every other view."""
    reporting, pop, context = prepare(session, query)
    rates = latest_rate_rows().subquery()
    reporting_rate = (
        select(rates.c.rate_to_usd)
        .where(rates.c.currency == reporting)
        .scalar_subquery()
    )
    annual = (
        CurrentCompensation.amount
        * 12
        / CompensationType.period_months
        * rates.c.rate_to_usd
        / reporting_rate
    )
    rows = session.execute(
        select(CompensationType.category, func.sum(annual))
        .select_from(pop)
        .join(CurrentCompensation, CurrentCompensation.employee_id == pop.c.employee_id)
        .join(
            CompensationType,
            CompensationType.id == CurrentCompensation.compensation_type_id,
        )
        .join(rates, rates.c.currency == pop.c.currency)
        .where(pop.c.total.is_not(None), CompensationType.counts_toward_total)
        .group_by(CompensationType.category)
        .order_by(func.sum(annual).desc())
    ).all()
    grand = sum((Decimal(a) for _, a in rows), Decimal(0))
    return context, [
        CompositionRow(
            category,
            money(amount),
            (Decimal(amount) / grand).quantize(Decimal("0.0001")),
        )
        for category, amount in rows
        if grand
    ]


# --- change report ---


@dataclass(frozen=True)
class ChangeEvent:
    employee_id: int
    code: str
    first_name: str
    last_name: str
    effective_date: date
    reasons: list[str]
    currency: str
    previous_total: Decimal | None  # annual CTC the day before, employee's currency
    new_total: Decimal  # annual CTC on the day
    percent_change: Decimal | None
    counts_as_increase: bool
    excluded_because: str | None  # "correction", "currency change", "no previous pay"


@dataclass(frozen=True)
class ChangeReport:
    date_from: date
    date_to: date
    employees_changed: int
    events: int
    events_counted: int
    average_increase: Decimal | None  # percent, over counted events
    items: list[ChangeEvent]  # newest first, at most `limit`


def change_report(
    session: Session,
    query: DirectoryQuery,
    date_from: date,
    date_to: date,
    limit: int,
) -> ChangeReport:
    """Who had a compensation change in [date_from, date_to], and the average increase.

    One event per employee per effective date. Its change is the employee's annual CTC
    from the latest record of each type on that date versus the day before, in their
    own currency (no exchange rates needed). An event counts toward the average
    increase unless it includes a correction, crosses a currency change (FR-7), or the
    employee had no pay before (a new hire). Dates after today haven't happened yet,
    so the range stops at today.
    """
    if date_from > date_to:
        raise ServiceError(Problem.INVALID, INVALID_RANGE)
    promote_due_records(session)
    date_to = min(date_to, utc_today())
    # Analytics population without the money: active, matching the filters.
    people = apply_filters(
        select(Employee.id.label("employee_id")).where(Employee.status == ACTIVE),
        replace(query, statuses=[]),
    ).subquery()

    events = (
        select(
            CompensationRecord.employee_id,
            CompensationRecord.effective_date.label("day"),
            array_agg(ChangeReason.code.distinct()).label("reasons"),
            func.bool_or(CompensationType.counts_toward_total).label("touches_ctc"),
        )
        .join(people, people.c.employee_id == CompensationRecord.employee_id)
        .join(ChangeReason, ChangeReason.id == CompensationRecord.change_reason_id)
        .join(
            CompensationType,
            CompensationType.id == CompensationRecord.compensation_type_id,
        )
        .where(CompensationRecord.effective_date.between(date_from, date_to))
        .group_by(CompensationRecord.employee_id, CompensationRecord.effective_date)
        .subquery("events")
    )

    # Each record with the date it stopped applying (the next record of the same type;
    # a later record on the same day replaces it the same day). Set-based, so one pass
    # over the history instead of a lookup per record.
    r = CompensationRecord
    valid = (
        select(
            r.employee_id,
            r.amount,
            r.currency,
            CompensationType.period_months,
            r.effective_date.label("start"),
            func.lead(r.effective_date)
            .over(
                partition_by=(r.employee_id, r.compensation_type_id),
                order_by=(r.effective_date, r.id),
            )
            .label("until"),
            CompensationType.counts_toward_total,
        )
        .join(people, people.c.employee_id == r.employee_id)
        .join(CompensationType, CompensationType.id == r.compensation_type_id)
        .where(r.effective_date <= date_to)
        .subquery("valid")
    )

    # Totals on the day and the day before, in one pass: a record applies on the day
    # if it started by then and wasn't replaced by then, and on the day before if it
    # started earlier and wasn't replaced earlier.
    annual = valid.c.amount * 12 / valid.c.period_months
    on_day = and_(
        valid.c.start <= events.c.day,
        valid.c.until.is_(None) | (valid.c.until > events.c.day),
    )
    day_before = and_(
        valid.c.start < events.c.day,
        valid.c.until.is_(None) | (valid.c.until >= events.c.day),
    )
    totals = (
        select(
            events.c.employee_id,
            events.c.day,
            events.c.reasons,
            events.c.touches_ctc,
            func.sum(annual).filter(day_before).label("before"),
            func.min(valid.c.currency).filter(day_before).label("before_currency"),
            func.sum(annual).filter(on_day).label("after"),
            func.min(valid.c.currency).filter(on_day).label("currency"),
        )
        .join(
            valid,
            and_(
                valid.c.employee_id == events.c.employee_id,
                valid.c.start <= events.c.day,
                valid.c.until.is_(None) | (valid.c.until >= events.c.day),
            ),
        )
        .where(valid.c.counts_toward_total)
        .group_by(
            events.c.employee_id, events.c.day, events.c.reasons, events.c.touches_ctc
        )
        .subquery("totals")
    )

    def has_any(codes) -> ColumnElement:
        return totals.c.reasons.overlap(cast(array(sorted(codes)), ARRAY(Text)))

    excluded = case(
        (has_any(NON_INCREASE_REASONS), literal("correction")),
        (~totals.c.touches_ctc, literal("outside total compensation")),
        (
            totals.c.before.is_(None) | (totals.c.before == 0),
            literal("no previous pay"),
        ),
        (totals.c.before_currency != totals.c.currency, literal("currency change")),
        (
            totals.c.reasons.contained_by(cast(array([RELOCATION]), ARRAY(Text)))
            & (totals.c.before == totals.c.after),
            literal("relocation without a pay change"),
        ),
        else_=None,
    )
    percent = case(
        (
            excluded.is_(None),
            (totals.c.after - totals.c.before) / totals.c.before * 100,
        ),
        else_=None,
    )
    detail = select(
        totals.c.employee_id,
        totals.c.day,
        totals.c.reasons,
        totals.c.currency,
        totals.c.before,
        totals.c.after,
        percent.label("percent"),
        excluded.label("excluded"),
    ).cte("detail")  # used twice below; Postgres computes it once

    summary_row = select(
        func.count(detail.c.employee_id.distinct()).label("employees_changed"),
        func.count().label("events"),
        func.count().filter(detail.c.excluded.is_(None)).label("counted"),
        func.avg(detail.c.percent).label("average"),
    ).subquery("summary")
    page = (
        select(detail, Employee.code, Employee.first_name, Employee.last_name)
        .join(Employee, Employee.id == detail.c.employee_id)
        .order_by(detail.c.day.desc(), Employee.last_name, Employee.id)
        .limit(limit)
        .subquery("page")
    )
    # One round trip: the summary, plus the first `limit` events (if any).
    result = session.execute(
        select(summary_row, page).select_from(summary_row).outerjoin(page, true())
    ).all()
    head = result[0]
    employees_changed, total_events, counted, average = (
        head.employees_changed,
        head.events,
        head.counted,
        head.average,
    )
    rows = [row for row in result if row.employee_id is not None]

    def pct(value) -> Decimal | None:
        return None if value is None else Decimal(value).quantize(CENT)

    return ChangeReport(
        date_from=date_from,
        date_to=date_to,
        employees_changed=employees_changed,
        events=total_events,
        events_counted=counted,
        average_increase=pct(average),
        items=[
            ChangeEvent(
                employee_id=row.employee_id,
                code=row.code,
                first_name=row.first_name,
                last_name=row.last_name,
                effective_date=row.day,
                reasons=sorted(row.reasons),
                currency=row.currency,
                previous_total=money(row.before),
                new_total=money(row.after),
                percent_change=pct(row.percent),
                counts_as_increase=row.excluded is None,
                excluded_because=row.excluded,
            )
            for row in rows
        ],
    )
