"""materialized view of each employee's annual total compensation

Revision ID: 0013_employee_totals
Revises: 0012_no_truncate_records
Create Date: 2026-10-17

Every pay-insight view needs each employee's annual total (counted types only, in the
employee's own currency). Computing it means aggregating all of current_compensation
(Scale Lab E11: ~2.3 s at 1M employees, in every view). The view stores it once; it is
refreshed after writes, by the daily job and at start-up, and a unique index lets it be
refreshed CONCURRENTLY, so readers are never blocked.

`refresh_employee_totals()` runs as its owner (SECURITY DEFINER): refreshing needs
ownership of the view, which the application's least-privilege role doesn't have.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_employee_totals"
down_revision: str | Sequence[str] | None = "0012_no_truncate_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE MATERIALIZED VIEW employee_totals AS
        SELECT cc.employee_id,
               sum(cc.amount * 12 / ct.period_months) AS total
          FROM current_compensation cc
          JOIN compensation_types ct ON ct.id = cc.compensation_type_id
         WHERE ct.counts_toward_total
         GROUP BY cc.employee_id
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_employee_totals_employee ON employee_totals (employee_id)")
    op.execute(
        """
        CREATE FUNCTION refresh_employee_totals() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        BEGIN
            REFRESH MATERIALIZED VIEW CONCURRENTLY employee_totals;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION refresh_employee_totals() FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP FUNCTION refresh_employee_totals()")
    op.execute("DROP MATERIALIZED VIEW employee_totals")
