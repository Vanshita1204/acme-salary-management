from sqlalchemy import BigInteger, Identity, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ChangeReason(Base):
    """One shared list of reasons a compensation record can carry, independent of type."""

    __tablename__ = "change_reasons"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )  # e.g. 'new_hire'
    label: Mapped[str] = mapped_column(Text, nullable=False)
