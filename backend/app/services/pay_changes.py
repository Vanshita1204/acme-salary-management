"""Stored pay-change events (Scale Lab E26; design in docs/DATABASE_DESIGN.md, "Derived data").

The pay-changes report needs, for each employee and each effective date in a period, the
annual total compensation the day before and on the day. Computing that from the history
windows over every compensation record on every request, which is slow. The per-day events
are computed once with `change_detail` (the single definition of an event) and stored in
`pay_change_events`, up to `pay_change_progress.filled_through`. The report reads them and
computes only the days after that live.

Maintenance:
- `backfill`: all history up to yesterday, once, when nothing has been stored yet.
- `fill_through_yesterday`: appends the days since the last fill (the daily job).
- `process_dirty`: recomputes the events of employees who received a *backdated* record
  (the database trigger lists them in `pay_change_dirty`); done before every stored read.

Everything here is derived data: it can be deleted and rebuilt from the history, and the
live computation (`change_detail` over the report's range) remains available.
"""

from datetime import date, timedelta

from sqlalchemy import (
    CHAR,
    BigInteger,
    Column,
    Date,
    MetaData,
    Numeric,
    Table,
    Text,
    delete,
    func,
    select,
    text,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.orm import Session

from app.models import CompensationRecord
from app.services.change_detail import change_detail
from app.services.compensation import utc_today

# Core tables on their own MetaData: derived data, not models the app creates or migrates.
_metadata = MetaData()
events = Table(
    "pay_change_events",
    _metadata,
    Column("employee_id", BigInteger, primary_key=True),
    Column("day", Date, primary_key=True),
    Column("reasons", ARRAY(Text)),
    Column("currency", CHAR(3)),
    Column("before_total", Numeric),
    Column("after_total", Numeric),
    Column("percent", Numeric),
    Column("excluded", Text),
)
progress = Table(
    "pay_change_progress",
    _metadata,
    Column("id", BigInteger, primary_key=True),
    Column("filled_through", Date),
)
dirty = Table(
    "pay_change_dirty",
    _metadata,
    Column("employee_id", BigInteger, primary_key=True),
    Column("from_day", Date),
)

EVENT_COLUMNS = (
    "employee_id",
    "day",
    "reasons",
    "currency",
    "before_total",
    "after_total",
    "percent",
    "excluded",
)
CHUNK_DAYS = 366  # a big backfill works a year at a time, to bound memory and time
SINGLE_PASS_RECORDS = 2_000_000  # up to this many records, one pass over the history
ONE_DAY = timedelta(days=1)
RACE_MARGIN = timedelta(
    minutes=5
)  # how far back the daily fill rechecks recent writers


def filled_through(session: Session) -> date | None:
    return session.scalar(select(progress.c.filled_through))


def set_filled_through(session: Session, day: date) -> None:
    session.execute(update(progress).values(filled_through=day))


def store_events(session: Session, detail) -> int:
    """Insert (or refresh) the events a `change_detail` query returns."""
    statement = insert(events).from_select(EVENT_COLUMNS, detail)
    statement = statement.on_conflict_do_update(
        index_elements=[events.c.employee_id, events.c.day],
        set_={name: statement.excluded[name] for name in EVENT_COLUMNS[2:]},
    )
    # preserve_rowcount: SQLAlchemy otherwise reports -1 for INSERT ... SELECT.
    result = session.execute(statement, execution_options={"preserve_rowcount": True})
    return result.rowcount


def backfill(
    session: Session, through: date | None = None, *, commit: bool = False
) -> int:
    """Compute and store every event up to `through` (default: yesterday), then record that
    they are stored. Safe to repeat: it refreshes rows rather than duplicating them. With
    `commit`, each year is committed as it is done (the progress only moves at the end, so
    the report ignores a half-finished backfill)."""
    through = through or utc_today() - ONE_DAY
    first, records = session.execute(
        select(func.min(CompensationRecord.effective_date), func.count())
    ).one()
    chunk = CHUNK_DAYS if records > SINGLE_PASS_RECORDS else None
    stored = 0
    start = first
    while start is not None and start <= through:
        end = (
            through
            if chunk is None
            else min(start + timedelta(days=chunk - 1), through)
        )
        stored += store_events(session, change_detail(start, end))
        if commit:
            session.commit()
        start = end + ONE_DAY
    set_filled_through(session, through)
    session.execute(delete(dirty))  # everything was just recomputed
    return stored


def fill_through_yesterday(session: Session) -> int:
    """Append the days since the last fill, up to yesterday (the daily job). Starts with a
    backfill if nothing has been stored yet."""
    target = utc_today() - ONE_DAY
    current = filled_through(session)
    if current is None:
        return backfill(session, target, commit=True)
    if current >= target:
        return 0
    started = session.scalar(select(func.clock_timestamp()))
    stored = store_events(
        session, change_detail(current + ONE_DAY, target, only_event_employees=True)
    )
    set_filled_through(session, target)
    # A backdated record inserted while this ran saw the old watermark (so was not listed)
    # and may not be in the snapshot above. Anyone who received a record in the last few
    # minutes is rechecked on the next report; normally that is nobody.
    session.execute(
        text(
            """
            INSERT INTO pay_change_dirty (employee_id, from_day)
            SELECT employee_id, min(effective_date)
              FROM compensation_records
             WHERE created_at >= :since AND effective_date <= :through
             GROUP BY employee_id
            ON CONFLICT (employee_id) DO UPDATE
                SET from_day = LEAST(pay_change_dirty.from_day, EXCLUDED.from_day)
            """
        ),
        {"since": started - RACE_MARGIN, "through": target},
    )
    return stored


def process_dirty(session: Session) -> int:
    """Recompute the events of employees who received a backdated record, from the earliest
    such record to `filled_through`. A new record never removes an event day, so refreshing
    the rows (an upsert) is enough. Returns how many employees were recomputed."""
    through = filled_through(session)
    if through is None:
        return 0
    taken = session.execute(
        delete(dirty).returning(dirty.c.employee_id, dirty.c.from_day)
    ).all()
    if not taken:
        return 0
    start = min(row.from_day for row in taken)
    ids = [row.employee_id for row in taken]
    store_events(session, change_detail(start, through, employee_ids=ids))
    return len(ids)


def stored_detail(session: Session, date_from: date, date_to: date, people):
    """The events of [date_from, date_to] for the employees in `people`: the stored days,
    then (if the range reaches past `filled_through`) the remaining days computed live.
    None when nothing has been stored yet, so the caller falls back to the live report."""
    through = filled_through(session)
    if through is None:
        return None
    process_dirty(session)
    parts = []
    stored_end = min(date_to, through)
    if date_from <= stored_end:
        parts.append(
            select(
                events.c.employee_id,
                events.c.day,
                events.c.reasons,
                events.c.currency,
                events.c.before_total.label("before"),
                events.c.after_total.label("after"),
                events.c.percent,
                events.c.excluded,
            )
            .join(people, people.c.employee_id == events.c.employee_id)
            .where(events.c.day.between(date_from, stored_end))
        )
    live_start = max(date_from, through + ONE_DAY)
    if live_start <= date_to:
        parts.append(
            change_detail(live_start, date_to, people=people, only_event_employees=True)
        )
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else union_all(*parts)
