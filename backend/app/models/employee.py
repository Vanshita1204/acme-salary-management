from datetime import date, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Sequence,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.company import Company
from app.models.country import Country
from app.models.currency import Currency
from app.models.org import Department, JobLevel, JobTitle

EMPLOYEE_STATUSES = ("active", "on_leave", "terminated")

# Backs system-generated employee codes ('EMP-000042'); never client-supplied.
employee_code_seq = Sequence("employee_code_seq", metadata=Base.metadata)


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        unique=True,
        server_default=text("employee_code(nextval('employee_code_seq'))"),  # see 0006
    )
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id"), nullable=False
    )
    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    department_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("departments.id"), nullable=False
    )
    job_title_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("job_titles.id"), nullable=False
    )
    job_level_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("job_levels.id"), nullable=False
    )
    current_country: Mapped[str] = mapped_column(
        CHAR(2), ForeignKey("countries.code"), nullable=False
    )
    # Authoritative current pay currency; independent of current_country.
    currency: Mapped[str] = mapped_column(CHAR(3), ForeignKey("currencies.code"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    hire_date: Mapped[date] = mapped_column(Date, nullable=False)
    termination_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    company: Mapped[Company] = relationship()
    # Tiny lookup tables: always loaded with the employee, so responses carry names.
    department_ref: Mapped[Department] = relationship(lazy="joined")
    job_title_ref: Mapped[JobTitle] = relationship(lazy="joined")
    job_level_ref: Mapped[JobLevel] = relationship(lazy="joined")
    country: Mapped[Country] = relationship()
    currency_ref: Mapped[Currency] = relationship()
    compensation_records: Mapped[list["CompensationRecord"]] = relationship(  # noqa: F821
        back_populates="employee", order_by="CompensationRecord.effective_date.desc()"
    )
    current_compensation: Mapped[list["CurrentCompensation"]] = relationship(  # noqa: F821
        back_populates="employee"
    )

    @property
    def department(self) -> str:
        return self.department_ref.name

    @property
    def job_title(self) -> str:
        return self.job_title_ref.name

    @property
    def job_level(self) -> str:
        return self.job_level_ref.code

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'on_leave', 'terminated')", name="chk_employee_status"
        ),
        CheckConstraint(
            "(status = 'terminated' AND termination_date IS NOT NULL)"
            " OR (status <> 'terminated' AND termination_date IS NULL)",
            name="chk_termination_date_matches_status",
        ),
        CheckConstraint(
            "termination_date IS NULL OR termination_date >= hire_date",
            name="chk_termination_after_hire",
        ),
        Index("ix_employees_company", "company_id"),
        Index("ix_employees_department", "department_id"),
        Index("ix_employees_country", "current_country"),
        Index("ix_employees_title", "job_title_id"),
        Index("ix_employees_level", "job_level_id"),
        Index("ix_employees_status", "status"),
        Index("ix_employees_hire_date", "hire_date", "id"),
        Index("ix_employees_name_search", func.lower(text("last_name")), func.lower(text("first_name"))),
    )
