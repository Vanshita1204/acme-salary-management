"""The definition of a pay-change event, as one query (shared by the live report and every fill).

An event is one employee on one effective date. Its change is the employee's annual CTC from
the latest record of each type on that date versus the day before, in their own currency (no
exchange rates needed). `excluded` says why an event does not count toward the average
increase (a correction, a currency change, no pay before, ...); it is NULL when it counts.

This query is the single source of that definition: the report runs it for its date range,
and `app.services.pay_changes` runs it to fill the stored events.
"""

from datetime import date

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Select,
    Text,
    and_,
    any_,
    bindparam,
    case,
    cast,
    func,
    literal,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, array, array_agg

from app.domain.compensation import NON_INCREASE_REASONS
from app.models import ChangeReason, CompensationRecord, CompensationType
from app.services.relocation import RELOCATION


def change_detail(
    date_from: date,
    date_to: date,
    *,
    people=None,
    employee_ids: list[int] | None = None,
    only_event_employees: bool = False,
) -> Select:
    """The events in [date_from, date_to] as rows of (employee_id, day, reasons, currency,
    before, after, percent, excluded).

    `people`: a subquery with an `employee_id` column restricting the employees (the report
    passes the active employees matching its filters; a fill passes none).
    `employee_ids`: only these employees (recomputing a few).
    `only_event_employees`: window only the history of employees who have an event in the
    range. Far cheaper when few do (one day, today's events), a little costlier when most do.
    """
    ids = (
        bindparam("employee_ids", employee_ids, type_=ARRAY(BigInteger))
        if employee_ids is not None
        else None
    )

    def restrict(stmt: Select, column) -> Select:
        if people is not None:
            stmt = stmt.join(people, people.c.employee_id == column)
        if ids is not None:
            stmt = stmt.where(column == any_(ids))
        return stmt

    events_query = restrict(
        select(
            CompensationRecord.employee_id,
            CompensationRecord.effective_date.label("day"),
            array_agg(ChangeReason.code.distinct()).label("reasons"),
            func.bool_or(CompensationType.counts_toward_total).label("touches_ctc"),
        ),
        CompensationRecord.employee_id,
    )
    events_query = (
        events_query.join(
            ChangeReason, ChangeReason.id == CompensationRecord.change_reason_id
        )
        .join(
            CompensationType,
            CompensationType.id == CompensationRecord.compensation_type_id,
        )
        .where(CompensationRecord.effective_date.between(date_from, date_to))
        .group_by(CompensationRecord.employee_id, CompensationRecord.effective_date)
    )
    # A CTE is computed once, which the restriction below needs; a subquery is inlined.
    events = (
        events_query.cte("events")
        if only_event_employees
        else events_query.subquery("events")
    )

    # Each record with the date it stopped applying (the next record of the same type;
    # a later record on the same day replaces it the same day). Set-based, so one pass
    # over the history instead of a lookup per record.
    r = CompensationRecord
    valid_query = restrict(
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
        ),
        r.employee_id,
    )
    valid_query = valid_query.join(
        CompensationType, CompensationType.id == r.compensation_type_id
    ).where(r.effective_date <= date_to)
    if only_event_employees:
        valid_query = valid_query.where(r.employee_id.in_(select(events.c.employee_id)))
    valid = valid_query.subquery("valid")

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
    return select(
        totals.c.employee_id,
        totals.c.day,
        cast(totals.c.reasons, ARRAY(Text)).label("reasons"),
        totals.c.currency,
        totals.c.before,
        totals.c.after,
        percent.label("percent"),
        excluded.label("excluded"),
    )
