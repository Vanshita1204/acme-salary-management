"""stored pay-change events, so the pay-changes report does not window the whole history

Revision ID: 0015_pay_change_events
Revises: 0014_trigram_search
Create Date: 2026-10-19

The report windows over every compensation record on each request: 7.5 s on the free tier
at 10,000 employees, 26-43 s at 1,000,000 (Scale Lab E16, E25). The per-day events are
computed once and stored here (design: docs/DATABASE_DESIGN.md, "Derived data"):

- pay_change_events: one row per employee per day on which pay changed, independent of any
  filter. Holds the days up to pay_change_progress.filled_through.
- pay_change_progress: exactly one row, the day up to which events are stored.
- pay_change_dirty: employees who received a *backdated* record (effective on or before
  filled_through), whose later events must be recomputed. Filled by a statement-level
  trigger, so every code path that inserts a record (a change, a relocation, an import, a
  seed) is covered and a bulk insert costs one statement.

All three are derived and can be rebuilt from compensation_records.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_pay_change_events"
down_revision: str | Sequence[str] | None = "0014_trigram_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE pay_change_events (
            employee_id  BIGINT  NOT NULL REFERENCES employees(id),
            day          DATE    NOT NULL,
            reasons      TEXT[]  NOT NULL,
            currency     CHAR(3) NOT NULL REFERENCES currencies(code),
            before_total NUMERIC,
            after_total  NUMERIC,
            percent      NUMERIC,
            excluded     TEXT CHECK (excluded IN (
                'correction', 'outside total compensation', 'no previous pay',
                'currency change', 'relocation without a pay change')),
            PRIMARY KEY (employee_id, day)
        )
        """
    )
    op.execute("CREATE INDEX ix_pay_change_events_day ON pay_change_events (day DESC, employee_id)")
    op.execute(
        """
        CREATE TABLE pay_change_progress (
            id             BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
            filled_through DATE
        )
        """
    )
    op.execute("INSERT INTO pay_change_progress (filled_through) VALUES (NULL)")
    op.execute(
        """
        CREATE TABLE pay_change_dirty (
            employee_id BIGINT PRIMARY KEY REFERENCES employees(id),
            from_day    DATE   NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION mark_pay_change_dirty() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        BEGIN
            INSERT INTO pay_change_dirty (employee_id, from_day)
            SELECT n.employee_id, min(n.effective_date)
              FROM new_rows n
             WHERE n.effective_date <= (SELECT filled_through FROM pay_change_progress)
             GROUP BY n.employee_id
            ON CONFLICT (employee_id) DO UPDATE
                SET from_day = LEAST(pay_change_dirty.from_day, EXCLUDED.from_day);
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_comp_records_pay_change_dirty
            AFTER INSERT ON compensation_records
            REFERENCING NEW TABLE AS new_rows
            FOR EACH STATEMENT EXECUTE FUNCTION mark_pay_change_dirty()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_comp_records_pay_change_dirty ON compensation_records")
    op.execute("DROP FUNCTION mark_pay_change_dirty()")
    op.execute("DROP TABLE pay_change_dirty")
    op.execute("DROP TABLE pay_change_progress")
    op.execute("DROP TABLE pay_change_events")
