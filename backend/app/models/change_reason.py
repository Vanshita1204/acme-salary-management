from sqlalchemy import BigInteger, ForeignKey, Identity, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.compensation_type import CompensationType


class ChangeReason(Base):
    """Valid change reasons, scoped per compensation type."""

    __tablename__ = "change_reasons"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    compensation_type_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("compensation_types.id"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. 'new_hire', 'correction'
    label: Mapped[str] = mapped_column(Text, nullable=False)

    compensation_type: Mapped[CompensationType] = relationship(back_populates="change_reasons")

    __table_args__ = (
        UniqueConstraint("compensation_type_id", "code", name="uq_change_reasons_type_code"),
        # Target of compensation_records' composite FK (type, reason).
        UniqueConstraint("compensation_type_id", "id", name="uq_change_reasons_type_id"),
    )
