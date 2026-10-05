"""Schemas for employees and their compensation records."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, EmailStr, Field

from app.schemas.catalog import NonEmpty, ORMModel

Status = Literal["active", "on_leave", "terminated"]
CountryCode = Annotated[
    str, Field(min_length=2, max_length=2), AfterValidator(str.upper)
]
CurrencyCode = Annotated[
    str, Field(min_length=3, max_length=3), AfterValidator(str.upper)
]


class EmployeeIn(BaseModel):
    """`code` is not accepted — the database generates it."""

    company_id: int
    first_name: NonEmpty
    last_name: NonEmpty
    email: EmailStr
    department: NonEmpty
    job_title: NonEmpty
    job_level: NonEmpty
    current_country: CountryCode
    currency: CurrencyCode
    status: Status = "active"
    hire_date: date
    termination_date: date | None = None


class EmployeeOut(ORMModel):
    id: int
    code: str
    company_id: int
    first_name: str
    last_name: str
    email: str
    department: str
    job_title: str
    job_level: str
    current_country: str
    currency: str
    status: Status
    hire_date: date
    termination_date: date | None
    created_at: datetime
    updated_at: datetime


class CompensationRecordIn(BaseModel):
    """Country and currency aren't accepted: a record is written in the employee's
    current country and currency (the DB trigger enforces the currency)."""

    compensation_type_id: int
    change_reason_id: int
    effective_date: date
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    note: str | None = None
    changed_by: NonEmpty


class CompensationRecordOut(ORMModel):
    id: int
    employee_id: int
    compensation_type_id: int
    change_reason_id: int
    effective_date: date
    country: str
    currency: str
    amount: Decimal
    note: str | None
    changed_by: str
    created_at: datetime


class DirectoryItem(BaseModel):
    id: int
    code: str
    first_name: str
    last_name: str
    email: str
    department: str
    job_title: str
    job_level: str
    current_country: str
    status: Status
    hire_date: date
    currency: str
    total_compensation: Decimal | None  # annual CTC, employee's currency
    total_compensation_reporting: Decimal | None  # annual CTC, reporting currency


class DirectoryPageOut(BaseModel):
    items: list[DirectoryItem]
    next_cursor: str | None
    prev_cursor: str | None
    reporting_currency: str
    rates_as_of: dict[str, date]
