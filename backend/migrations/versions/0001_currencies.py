"""create currencies

Revision ID: 0001_currencies
Revises: None
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0001_currencies'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "currencies",
        sa.Column("code", sa.CHAR(3), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("currencies")
