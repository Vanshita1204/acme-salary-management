"""departments, job titles and job levels as reference data

Revision ID: 0011_org_structure
Revises: 0010_future_dated_records
Create Date: 2026-10-05

Replaces the free-text employees.department / job_title / job_level columns with
foreign keys to lookup tables (Phase 7A). Existing data is backfilled in place:
distinct trimmed values become rows (case variants merge, since names are CITEXT
UNIQUE) and every employee points at the matching row. Downgrade restores the text
columns from the joins.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT

# revision identifiers, used by Alembic.
revision: str = "0011_org_structure"
down_revision: str | Sequence[str] | None = "0010_future_dated_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Level codes like 'L3' get the label 'Level 3' (same rule as app.seed.org_structure).
LEVEL_LABEL = (
    "CASE WHEN code ~* '^L[0-9]+$'"
    " THEN 'Level ' || substring(code FROM '[0-9]+') ELSE code END"
)


def created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", CITEXT(), nullable=False, unique=True),
        created_at(),
    )
    op.create_table(
        "job_titles",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", CITEXT(), nullable=False, unique=True),
        created_at(),
    )
    op.create_table(
        "job_levels",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("code", CITEXT(), nullable=False, unique=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False, unique=True),
        created_at(),
    )

    # Backfill from existing employees. ORDER BY makes the surviving spelling of a
    # case-variant group deterministic.
    op.execute(
        """
        INSERT INTO departments (name)
        SELECT DISTINCT btrim(department) FROM employees ORDER BY 1
        ON CONFLICT (name) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO job_titles (name)
        SELECT DISTINCT btrim(job_title) FROM employees ORDER BY 1
        ON CONFLICT (name) DO NOTHING
        """
    )
    # Rank by the code's number (L2 before L10), then alphabetically.
    op.execute(
        f"""
        INSERT INTO job_levels (code, label, rank)
        SELECT code, {LEVEL_LABEL},
               row_number() OVER (
                   ORDER BY substring(code FROM '[0-9]+')::int NULLS LAST, code
               )
        FROM (
            SELECT DISTINCT ON (lower(btrim(job_level))) btrim(job_level) AS code
            FROM employees
            ORDER BY lower(btrim(job_level)), btrim(job_level)
        ) AS levels
        """
    )

    for column, table in (
        ("department_id", "departments"),
        ("job_title_id", "job_titles"),
        ("job_level_id", "job_levels"),
    ):
        op.add_column(
            "employees",
            sa.Column(column, sa.BigInteger(), sa.ForeignKey(f"{table}.id")),
        )
    op.execute(
        """
        UPDATE employees e
        SET department_id = d.id, job_title_id = t.id, job_level_id = l.id
        FROM departments d, job_titles t, job_levels l
        WHERE d.name = btrim(e.department)::citext
          AND t.name = btrim(e.job_title)::citext
          AND l.code = btrim(e.job_level)::citext
        """
    )
    for column in ("department_id", "job_title_id", "job_level_id"):
        op.alter_column("employees", column, nullable=False)

    op.drop_index("ix_employees_department", table_name="employees")
    op.drop_index("ix_employees_title", table_name="employees")
    op.drop_index("ix_employees_level", table_name="employees")
    for column in ("department", "job_title", "job_level"):
        op.drop_column("employees", column)
    op.create_index("ix_employees_department", "employees", ["department_id"])
    op.create_index("ix_employees_title", "employees", ["job_title_id"])
    op.create_index("ix_employees_level", "employees", ["job_level_id"])


def downgrade() -> None:
    for column in ("department", "job_title", "job_level"):
        op.add_column("employees", sa.Column(column, sa.Text()))
    op.execute(
        """
        UPDATE employees e
        SET department = d.name, job_title = t.name, job_level = l.code
        FROM departments d, job_titles t, job_levels l
        WHERE d.id = e.department_id
          AND t.id = e.job_title_id
          AND l.id = e.job_level_id
        """
    )
    for column in ("department", "job_title", "job_level"):
        op.alter_column("employees", column, nullable=False)

    op.drop_index("ix_employees_department", table_name="employees")
    op.drop_index("ix_employees_title", table_name="employees")
    op.drop_index("ix_employees_level", table_name="employees")
    for column in ("department_id", "job_title_id", "job_level_id"):
        op.drop_column("employees", column)  # drops its FK with it
    op.create_index("ix_employees_department", "employees", ["department"])
    op.create_index("ix_employees_title", "employees", ["job_title"])
    op.create_index("ix_employees_level", "employees", ["job_level"])

    op.drop_table("job_levels")
    op.drop_table("job_titles")
    op.drop_table("departments")
