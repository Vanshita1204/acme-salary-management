"""create compensation_records with validation and append-only triggers

Revision ID: 0007_compensation_records
Revises: 0006_employees
Create Date: 2026-10-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0007_compensation_records'
down_revision: str | Sequence[str] | None = '0006_employees'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compensation_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("employee_id", sa.BigInteger(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column(
            "compensation_type_id",
            sa.BigInteger(),
            sa.ForeignKey("compensation_types.id"),
            nullable=False,
        ),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("country", sa.CHAR(2), sa.ForeignKey("countries.code"), nullable=False),
        sa.Column("currency", sa.CHAR(3), sa.ForeignKey("currencies.code"), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "change_reason_id",
            sa.BigInteger(),
            sa.ForeignKey("change_reasons.id"),
            nullable=False,
        ),
        sa.Column("note", sa.Text()),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("amount >= 0", name="chk_comp_record_amount_non_negative"),
    )
    op.create_index(
        "ix_comp_records_employee_type_effdate",
        "compensation_records",
        ["employee_id", "compensation_type_id", sa.text("effective_date DESC")],
    )

    op.execute(
        """
        CREATE FUNCTION validate_compensation_record() RETURNS TRIGGER AS $$
        DECLARE
            v_hire_date DATE;
            v_currency CHAR(3);
            v_is_base_pay BOOLEAN;
        BEGIN
            SELECT hire_date, currency INTO v_hire_date, v_currency
            FROM employees WHERE id = NEW.employee_id;

            SELECT is_base_pay INTO v_is_base_pay
            FROM compensation_types WHERE id = NEW.compensation_type_id;

            IF NEW.effective_date < v_hire_date THEN
                RAISE EXCEPTION 'effective_date cannot precede hire_date';
            END IF;

            IF NEW.currency <> v_currency THEN
                RAISE EXCEPTION 'new compensation records must use employee %''s current currency (%), got %',
                    NEW.employee_id, v_currency, NEW.currency;
            END IF;

            IF v_is_base_pay AND NEW.amount <= 0 THEN
                RAISE EXCEPTION 'base-pay compensation must be greater than zero';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_compensation_record
            BEFORE INSERT ON compensation_records
            FOR EACH ROW EXECUTE FUNCTION validate_compensation_record()
        """
    )

    op.execute(
        """
        CREATE FUNCTION forbid_mutation() RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_comp_records_no_update
            BEFORE UPDATE OR DELETE ON compensation_records
            FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("compensation_records")  # drops both triggers with it
    op.execute("DROP FUNCTION forbid_mutation()")
    op.execute("DROP FUNCTION validate_compensation_record()")
