"""index future-dated compensation records

Revision ID: 0010_future_dated_records
Revises: 0009_exchange_rates
Create Date: 2026-10-05

A partial index over records written with an effective date after the day they were
created. These are the only records that can become current later on (FR-4), so
promoting them on read scans this small index instead of the whole history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_future_dated_records"
down_revision: str | Sequence[str] | None = "0009_exchange_rates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_comp_records_future_dated",
        "compensation_records",
        ["effective_date"],
        postgresql_where=sa.text(
            "effective_date > (created_at AT TIME ZONE 'UTC')::date"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_comp_records_future_dated", table_name="compensation_records")
