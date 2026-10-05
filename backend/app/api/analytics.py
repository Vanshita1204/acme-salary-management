"""Pay insights (§5). Every view takes the directory's search and filters plus
`reporting_currency`, and covers active employees only."""

from dataclasses import asdict
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.api.employees import DirectoryView
from app.domain.analytics import MIN_PEER_GROUP_SIZE, OUTLIER_HIGH, OUTLIER_LOW
from app.schemas.analytics import (
    ChangeReportOut,
    CompositionOut,
    HistogramOut,
    OutliersOut,
    RoleByCountryOut,
    StatsOut,
    SummaryOut,
)
from app.services import analytics

router = APIRouter(prefix="/analytics", tags=["analytics"])


def context(ctx: analytics.Context) -> dict:
    return asdict(ctx)


@router.get("/summary", response_model=SummaryOut)
def summary(db: DbSession, view: DirectoryView) -> SummaryOut:
    """Headcount and annual cost overall, with a breakdown by country and department."""
    result = analytics.summary(db, view)
    out = SummaryOut(
        **context(result.context),
        headcount=result.headcount,
        total_cost=result.total_cost,
        average=result.average,
        median=result.median,
        by_country=result.by_country,
        by_department=result.by_department,
    )
    db.commit()  # keep future-dated records promoted while reading
    return out


@router.get("/stats", response_model=StatsOut)
def grouped_stats(
    db: DbSession,
    view: DirectoryView,
    group_by: Literal["department", "country", "title", "level"] = "department",
) -> StatsOut:
    """Average, median, minimum and maximum annual pay per group."""
    ctx, rows = analytics.grouped_stats(db, view, group_by)
    out = StatsOut(**context(ctx), group_by=group_by, groups=rows)
    db.commit()
    return out


@router.get("/role-by-country", response_model=RoleByCountryOut)
def role_by_country(db: DbSession, view: DirectoryView) -> RoleByCountryOut:
    """Pay for the same title and level across countries. Narrow to one role with
    `job_title_id` and `job_level_id`."""
    ctx, rows = analytics.role_by_country(db, view)
    out = RoleByCountryOut(**context(ctx), rows=rows)
    db.commit()
    return out


@router.get("/histogram", response_model=HistogramOut)
def histogram(
    db: DbSession,
    view: DirectoryView,
    bins: Annotated[int, Query(ge=1, le=analytics.MAX_BINS)] = 20,
) -> HistogramOut:
    """Distribution of annual pay in equal-width bins."""
    ctx, result = analytics.histogram(db, view, bins)
    out = HistogramOut(**context(ctx), bins=result)
    db.commit()
    return out


@router.get("/outliers", response_model=OutliersOut)
def outliers(db: DbSession, view: DirectoryView) -> OutliersOut:
    """Employees paid well below or above peers with the same title, level and
    country (peer groups of at least 5)."""
    ctx, rows = analytics.outliers(db, view)
    out = OutliersOut(
        **context(ctx),
        low_threshold=OUTLIER_LOW,
        high_threshold=OUTLIER_HIGH,
        min_peer_group_size=MIN_PEER_GROUP_SIZE,
        outliers=rows,
    )
    db.commit()
    return out


@router.get("/composition", response_model=CompositionOut)
def composition(db: DbSession, view: DirectoryView) -> CompositionOut:
    """Share of total compensation per compensation-type category."""
    ctx, rows = analytics.composition(db, view)
    out = CompositionOut(**context(ctx), categories=rows)
    db.commit()
    return out


@router.get("/changes", response_model=ChangeReportOut)
def change_report(
    db: DbSession,
    view: DirectoryView,
    date_from: date,
    date_to: date,
    limit: Annotated[int, Query(ge=0, le=1000)] = 200,
) -> ChangeReportOut:
    """Compensation changes effective in [date_from, date_to] and the average
    increase (corrections, currency changes and new hires excluded). `items` is the
    newest `limit` events; the counts and average cover all of them."""
    report = analytics.change_report(db, view, date_from, date_to, limit)
    out = ChangeReportOut(**asdict(report))
    db.commit()
    return out
