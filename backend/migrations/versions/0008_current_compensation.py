"""create current_compensation

Revision ID: 0008_current_compensation
Revises: 0007_compensation_records
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0008_current_compensation'
down_revision: str | Sequence[str] | None = '0007_compensation_records'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "current_compensation",
        sa.Column("employee_id", sa.BigInteger(), sa.ForeignKey("employees.id"), primary_key=True),
        sa.Column(
            "compensation_type_id",
            sa.BigInteger(),
            sa.ForeignKey("compensation_types.id"),
            primary_key=True,
        ),
        sa.Column(
            "compensation_record_id",
            sa.BigInteger(),
            sa.ForeignKey("compensation_records.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_current_comp_type", "current_compensation", ["compensation_type_id"])


def downgrade() -> None:
    op.drop_table("current_compensation")
