"""create change_reasons

Revision ID: 0005_change_reasons
Revises: 0004_compensation_types
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005_change_reasons'
down_revision: str | Sequence[str] | None = '0004_compensation_types'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_reasons",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        # One shared list: any record of any compensation type can use any reason.
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("label", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("change_reasons")
