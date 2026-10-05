"""create exchange_rates

Revision ID: 0009_exchange_rates
Revises: 0008_current_compensation
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0009_exchange_rates'
down_revision: str | Sequence[str] | None = '0008_current_compensation'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exchange_rates",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("currency", sa.CHAR(3), sa.ForeignKey("currencies.code"), nullable=False),
        # USD value of one unit; 16 dp keeps ~9 significant digits even for IRR (~1.5M per USD).
        sa.Column("rate_to_usd", sa.Numeric(24, 16), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("rate_to_usd > 0", name="chk_rate_to_usd_positive"),
        sa.UniqueConstraint("currency", "rate_date", name="uq_exchange_rates_currency_date"),
    )
    op.create_index(
        "ix_exchange_rates_latest", "exchange_rates", ["currency", sa.text("rate_date DESC")]
    )


def downgrade() -> None:
    op.drop_table("exchange_rates")
