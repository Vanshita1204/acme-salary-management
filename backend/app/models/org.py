"""Org structure reference data: departments, job titles and job levels.

Lookup tables rather than free text on employees, so "Engineering" and "engineering"
can't become two departments (SPECIFICATION §1). Names are CITEXT: unique ignoring case.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, Integer, Text, func
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JobTitle(Base):
    """Universal, not tied to a department: "Analyst" exists in Finance and Operations."""

    __tablename__ = "job_titles"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JobLevel(Base):
    """`rank` orders levels (L2 before L10); `code` is what HR sees."""

    __tablename__ = "job_levels"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)  # e.g. 'L3'
    label: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. 'Level 3'
    rank: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
