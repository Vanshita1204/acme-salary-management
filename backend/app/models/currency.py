from sqlalchemy import CHAR, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Currency(Base):
    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(CHAR(3), primary_key=True)  # ISO 4217
    name: Mapped[str] = mapped_column(Text, nullable=False)
