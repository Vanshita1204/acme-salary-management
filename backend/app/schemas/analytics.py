"""Responses for the pay-insight views (§5). Row types are the service dataclasses."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from app.services.analytics import (
    Bin,
    ChangeEvent,
    CompositionRow,
    CostRow,
    OutlierRow,
    RoleCountryRow,
    StatsRow,
)


class AnalyticsOut(BaseModel):
    """Every view says which currency it's in and the date of every rate it used."""

    reporting_currency: str
    rates_as_of: dict[str, date]
    excluded_no_rate: (
        int  # active matching employees left out: no rate for their currency
    )


class SummaryOut(AnalyticsOut):
    headcount: int
    total_cost: Decimal | None  # annual, reporting currency
    average: Decimal | None
    median: Decimal | None
    by_country: list[CostRow]
    by_department: list[CostRow]


class StatsOut(AnalyticsOut):
    group_by: str
    groups: list[StatsRow]


class RoleByCountryOut(AnalyticsOut):
    rows: list[RoleCountryRow]


class HistogramOut(AnalyticsOut):
    bins: list[Bin]


class OutliersOut(AnalyticsOut):
    low_threshold: Decimal
    high_threshold: Decimal
    min_peer_group_size: int
    outliers: list[OutlierRow]


class CompositionOut(AnalyticsOut):
    categories: list[CompositionRow]


class ChangeReportOut(BaseModel):
    """In each employee's own currency, so no exchange rates are involved."""

    date_from: date
    date_to: date  # capped at today
    employees_changed: int
    events: int
    events_counted: int
    average_increase: Decimal | None  # percent
    items: list[ChangeEvent]
