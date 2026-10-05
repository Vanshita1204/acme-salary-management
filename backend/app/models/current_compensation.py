from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.compensation_record import CompensationRecord
from app.models.compensation_type import CompensationType


class CurrentCompensation(Base):
    """Latest record per (employee, compensation type); refreshed by the service layer
    in the same transaction that inserts the compensation_records row."""

    __tablename__ = "current_compensation"

    employee_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id"), primary_key=True
    )
    compensation_type_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("compensation_types.id"), primary_key=True
    )
    compensation_record_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("compensation_records.id"), nullable=False, unique=True
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    employee: Mapped["Employee"] = relationship(back_populates="current_compensation")  # noqa: F821
    compensation_type: Mapped[CompensationType] = relationship()
    compensation_record: Mapped[CompensationRecord] = relationship()

    __table_args__ = (Index("ix_current_comp_type", "compensation_type_id"),)
