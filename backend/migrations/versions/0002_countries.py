"""create countries

Revision ID: 0002_countries
Revises: 0001_currencies
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002_countries'
down_revision: str | Sequence[str] | None = '0001_currencies'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "countries",
        sa.Column("code", sa.CHAR(2), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "default_currency", sa.CHAR(3), sa.ForeignKey("currencies.code"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("countries")
