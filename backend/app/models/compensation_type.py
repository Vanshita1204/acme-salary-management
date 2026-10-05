from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class CompensationType(Base):
    """Universal catalog of compensation types, shared by every company."""

    __tablename__ = "compensation_types"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)  # free text, e.g. 'fixed', 'bonus'
    subtype: Mapped[str | None] = mapped_column(Text)  # free text, e.g. 'housing'
    # Months the recorded amount covers: annual_amount = amount * 12 / period_months.
    period_months: Mapped[int] = mapped_column(Integer, nullable=False)
    is_base_pay: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # Part of total compensation (CTC)? E.g. an internet reimbursement may or may not be.
    counts_toward_total: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true()
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("period_months > 0", name="chk_period_months_positive"),
        CheckConstraint(
            "NOT is_base_pay OR counts_toward_total", name="chk_base_pay_counts_toward_total"
        ),
        UniqueConstraint("category", "subtype", name="uq_comp_types_category_subtype"),
        # At most one base-pay type, globally.
        Index(
            "uq_one_base_pay_type",
            text("(true)"),
            unique=True,
            postgresql_where=text("is_base_pay"),
        ),
        Index("ix_comp_types_category", "category"),
    )
