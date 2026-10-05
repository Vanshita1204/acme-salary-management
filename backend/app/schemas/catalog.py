"""Schemas for reference data and the compensation catalog."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# Machine-friendly identifiers such as reason codes: 'bonus_payout', 'new_hire'.
# Normalized before the pattern check, so ' Spot_Award ' is accepted as 'spot_award'.
Code = Annotated[
    str,
    BeforeValidator(lambda v: v.strip().lower() if isinstance(v, str) else v),
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*$"),
]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CurrencyOut(ORMModel):
    code: str
    name: str


class CountryOut(ORMModel):
    code: str
    name: str
    default_currency: str


class CompanyIn(BaseModel):
    name: NonEmpty


class CompanyOut(ORMModel):
    id: int
    name: str


class CompensationTypeIn(BaseModel):
    """`is_base_pay` is deliberately absent: the single base-pay type is seeded, never created."""

    model_config = ConfigDict(extra="forbid")

    name: NonEmpty
    category: NonEmpty
    subtype: NonEmpty | None = None
    period_months: int = Field(gt=0, description="1 monthly, 3 quarterly, 12 annual, …")
    counts_toward_total: bool = Field(
        default=True, description="Include in total compensation (CTC)"
    )


class CompensationTypeOut(ORMModel):
    id: int
    name: str
    category: str
    subtype: str | None
    period_months: int
    is_base_pay: bool
    counts_toward_total: bool
    created_at: datetime


class ChangeReasonIn(BaseModel):
    code: Code
    label: NonEmpty


class ChangeReasonOut(ORMModel):
    id: int
    code: str
    label: str


# --- org structure (Phase 7A) ---


class DepartmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonEmpty


class DepartmentOut(ORMModel):
    id: int
    name: str


class JobTitleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonEmpty


class JobTitleOut(ORMModel):
    id: int
    name: str


class JobLevelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: NonEmpty
    label: NonEmpty
    rank: int = Field(description="Order of seniority; unique, lower is more junior")


class JobLevelUpdate(BaseModel):
    """Levels are labels: renaming or re-ranking doesn't change any pay or history."""

    model_config = ConfigDict(extra="forbid")

    code: NonEmpty | None = None
    label: NonEmpty | None = None
    rank: int | None = None


class JobLevelOut(ORMModel):
    id: int
    code: str
    label: str
    rank: int
