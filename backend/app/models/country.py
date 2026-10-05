from sqlalchemy import CHAR, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.currency import Currency


class Country(Base):
    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(CHAR(2), primary_key=True)  # ISO 3166-1 alpha-2
    name: Mapped[str] = mapped_column(Text, nullable=False)
    default_currency: Mapped[str] = mapped_column(
        CHAR(3), ForeignKey("currencies.code"), nullable=False
    )

    default_currency_ref: Mapped[Currency] = relationship()
