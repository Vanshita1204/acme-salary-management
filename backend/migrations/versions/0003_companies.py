"""create companies

Revision ID: 0003_companies
Revises: 0002_countries
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003_companies'
down_revision: str | Sequence[str] | None = '0002_countries'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
    )


def downgrade() -> None:
    op.drop_table("companies")
