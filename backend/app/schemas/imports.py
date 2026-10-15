"""Schemas for the CSV employee import (FR-5)."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class RowErrorOut(BaseModel):
    row: int  # spreadsheet numbering: the header is row 1
    column: str
    reason: str


class PayAmountOut(BaseModel):
    type_id: int
    name: str
    amount: Decimal


class ImportRowOut(BaseModel):
    row: int
    first_name: str
    last_name: str
    email: str
    company_id: int
    department_id: int
    job_title_id: int
    job_level_id: int
    country: str
    hire_date: date
    currency: str
    pay: list[PayAmountOut]  # base pay first, then each other component given


class ValidationReport(BaseModel):
    ok: bool  # true only when every row is valid; confirm saves nothing otherwise
    row_count: int  # data rows that passed validation
    errors: list[RowErrorOut]
    # Companies named in the file that don't exist yet (each is also a row error). The
    # app can create them on request, then the file is checked again.
    unknown_companies: list[str] = []
    preview: list[
        ImportRowOut
    ]  # the first PREVIEW_LIMIT valid rows, as they'd be saved


class ImportConfirmed(BaseModel):
    created: int
    employee_ids: list[int]
