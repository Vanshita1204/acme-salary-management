"""forbid TRUNCATE on compensation_records

Revision ID: 0012_no_truncate_records
Revises: 0011_org_structure
Create Date: 2026-10-05

The append-only trigger (0007) fires per row, so it stops UPDATE and DELETE but not
TRUNCATE, which empties a table without visiting any rows. A statement-level trigger
closes that: it also fires for tables emptied through `TRUNCATE employees CASCADE`.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_no_truncate_records"
down_revision: str | Sequence[str] | None = "0011_org_structure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_comp_records_no_truncate
            BEFORE TRUNCATE ON compensation_records
            FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_comp_records_no_truncate ON compensation_records")
