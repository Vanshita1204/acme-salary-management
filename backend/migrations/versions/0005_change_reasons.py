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
        sa.Column(
            "compensation_type_id",
            sa.BigInteger(),
            sa.ForeignKey("compensation_types.id"),
            nullable=False,
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.UniqueConstraint("compensation_type_id", "code", name="uq_change_reasons_type_code"),
        # Target of compensation_records' composite FK (type, reason).
        sa.UniqueConstraint("compensation_type_id", "id", name="uq_change_reasons_type_id"),
    )


def downgrade() -> None:
    op.drop_table("change_reasons")
