from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Numeric,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.change_reason import ChangeReason
from app.models.compensation_type import CompensationType


class CompensationRecord(Base):
    """Append-only compensation history: one row per (employee, type, effective date).

    Hire-date, current-currency and base-pay > 0 rules plus the no-update/no-delete
    guarantee are DB triggers created in the migration, not modeled here.
    """

    __tablename__ = "compensation_records"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id"), nullable=False
    )
    compensation_type_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("compensation_types.id"), nullable=False
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    country: Mapped[str] = mapped_column(CHAR(2), ForeignKey("countries.code"), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), ForeignKey("currencies.code"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    change_reason_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(Text, nullable=False)  # a label, not a verified identity
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    employee: Mapped["Employee"] = relationship(back_populates="compensation_records")  # noqa: F821
    compensation_type: Mapped[CompensationType] = relationship()
    change_reason: Mapped[ChangeReason] = relationship(
        foreign_keys=[change_reason_id],
        primaryjoin="CompensationRecord.change_reason_id == ChangeReason.id",
        viewonly=True,
    )

    __table_args__ = (
        CheckConstraint("amount >= 0", name="chk_comp_record_amount_non_negative"),
        # A reason must belong to the record's compensation type.
        ForeignKeyConstraint(
            ["compensation_type_id", "change_reason_id"],
            ["change_reasons.compensation_type_id", "change_reasons.id"],
            name="fk_comp_records_type_reason",
        ),
        Index(
            "ix_comp_records_employee_type_effdate",
            "employee_id",
            "compensation_type_id",
            effective_date.desc(),
        ),
    )
