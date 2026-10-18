"""trigram indexes for the directory's text search

Revision ID: 0014_trigram_search
Revises: 0013_employee_totals
Create Date: 2026-10-18

The directory searches full name, last name, email and employee code with ILIKE
'%term%', which no B-tree can serve: at 1M employees a search of 3 or more characters
that matches few people read every row (1.6-2.4 s, Scale Lab E13). A pg_trgm GIN index per
searched expression lets Postgres find the matching rows directly. The expressions are
exactly those `app.services.directory.apply_filters` builds, or the planner won't use
them. Terms shorter than 3 characters still scan: trigrams need 3. At 10,000 employees a
search needs no index (15-26 ms), so the migration is deliberately forgiving.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_trigram_search"
down_revision: str | Sequence[str] | None = "0013_employee_totals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = {
    "ix_employees_trgm_full_name": "(first_name || ' ' || last_name)",
    "ix_employees_trgm_last_name": "last_name",
    "ix_employees_trgm_email": "((email)::varchar)",
    "ix_employees_trgm_code": "code",
}


def upgrade() -> None:
    # The indexes are an optimisation, not something the app needs to run: where the
    # extension cannot be created (a managed database that doesn't allow it), skip them
    # instead of failing the whole migration, and with it the app's start-up.
    definitions = "; ".join(
        f"EXECUTE $q$CREATE INDEX {name} ON employees USING gin ({expression} gin_trgm_ops)$q$"
        for name, expression in INDEXES.items()
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
            {definitions};
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'trigram search indexes skipped: %', SQLERRM;
        END
        $$
        """
    )


def downgrade() -> None:
    for name in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
