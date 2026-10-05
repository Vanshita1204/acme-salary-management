"""create employees

Revision ID: 0006_employees
Revises: 0005_change_reasons
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0006_employees'
down_revision: str | Sequence[str] | None = '0005_change_reasons'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    # Backs system-generated employee codes ('EMP-000042'); never client-supplied.
    op.execute("CREATE SEQUENCE employee_code_seq")
    op.create_table(
        "employees",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "code",
            sa.String(12),
            nullable=False,
            unique=True,
            server_default=sa.text("'EMP-' || lpad(nextval('employee_code_seq')::text, 6, '0')"),
        ),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("last_name", sa.Text(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False, unique=True),
        sa.Column("department", sa.Text(), nullable=False),
        sa.Column("job_title", sa.Text(), nullable=False),
        sa.Column("job_level", sa.Text(), nullable=False),
        sa.Column(
            "current_country", sa.CHAR(2), sa.ForeignKey("countries.code"), nullable=False
        ),
        sa.Column("currency", sa.CHAR(3), sa.ForeignKey("currencies.code"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("hire_date", sa.Date(), nullable=False),
        sa.Column("termination_date", sa.Date()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('active', 'on_leave', 'terminated')", name="chk_employee_status"
        ),
        sa.CheckConstraint(
            "(status = 'terminated' AND termination_date IS NOT NULL)"
            " OR (status <> 'terminated' AND termination_date IS NULL)",
            name="chk_termination_date_matches_status",
        ),
        sa.CheckConstraint(
            "termination_date IS NULL OR termination_date >= hire_date",
            name="chk_termination_after_hire",
        ),
    )
    op.execute("ALTER SEQUENCE employee_code_seq OWNED BY employees.code")
    op.create_index("ix_employees_company", "employees", ["company_id"])
    op.create_index("ix_employees_department", "employees", ["department"])
    op.create_index("ix_employees_country", "employees", ["current_country"])
    op.create_index("ix_employees_title", "employees", ["job_title"])
    op.create_index("ix_employees_level", "employees", ["job_level"])
    op.create_index("ix_employees_status", "employees", ["status"])
    op.create_index("ix_employees_hire_date", "employees", ["hire_date", "id"])
    op.create_index(
        "ix_employees_name_search",
        "employees",
        [sa.text("lower(last_name)"), sa.text("lower(first_name)")],
    )


def downgrade() -> None:
    op.drop_table("employees")  # also drops employee_code_seq (OWNED BY)
    # citext extension is left installed; other objects may depend on it.
