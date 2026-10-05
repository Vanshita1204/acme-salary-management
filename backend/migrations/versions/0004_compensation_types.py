"""create compensation_types

Revision ID: 0004_compensation_types
Revises: 0003_companies
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0004_compensation_types'
down_revision: str | Sequence[str] | None = '0003_companies'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compensation_types",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("subtype", sa.Text()),
        sa.Column("period_months", sa.Integer(), nullable=False),
        sa.Column("is_base_pay", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Whether this type is part of total compensation (CTC). Lets an employer put e.g.
        # an internet reimbursement inside CTC while another keeps it outside.
        sa.Column(
            "counts_toward_total", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("period_months > 0", name="chk_period_months_positive"),
        sa.CheckConstraint(
            "NOT is_base_pay OR counts_toward_total", name="chk_base_pay_counts_toward_total"
        ),
        sa.UniqueConstraint("category", "subtype", name="uq_comp_types_category_subtype"),
    )
    # At most one base-pay type, globally.
    op.create_index(
        "uq_one_base_pay_type",
        "compensation_types",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("is_base_pay"),
    )
    op.create_index("ix_comp_types_category", "compensation_types", ["category"])


def downgrade() -> None:
    op.drop_table("compensation_types")
