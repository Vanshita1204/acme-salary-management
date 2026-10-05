from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbSession
from app.domain.currency import MissingRateError, convert, cross_rate
from app.models import Currency
from app.services.exchange_rates import latest_rates, refresh_exchange_rates

router = APIRouter(prefix="/exchange-rates", tags=["exchange-rates"])

CurrencyCode = Annotated[
    str, Query(min_length=3, max_length=3, pattern="^[A-Za-z]{3}$")
]


class ConversionOut(BaseModel):
    amount: Decimal
    from_currency: str
    to_currency: str
    converted: Decimal
    rate: Decimal  # units of to_currency per one from_currency
    rates_as_of: dict[str, date]


class RateOut(BaseModel):
    currency: str
    rate_to_usd: Decimal
    rate_date: date


class RefreshOut(BaseModel):
    rate_date: date
    skipped: bool  # true when stored rates were fresh and the provider wasn't called
    stored: int
    missing: list[str]


@router.get("/convert", response_model=ConversionOut)
def convert_amount(
    amount: Annotated[Decimal, Query(ge=0, max_digits=14, decimal_places=2)],
    from_currency: Annotated[CurrencyCode, Query(alias="from")],
    to_currency: Annotated[CurrencyCode, Query(alias="to")],
    db: DbSession,
) -> ConversionOut:
    """Convert using the latest stored rates — never calls the provider at request time."""
    from_currency, to_currency = from_currency.upper(), to_currency.upper()
    supported = set(
        db.scalars(
            select(Currency.code).where(Currency.code.in_([from_currency, to_currency]))
        )
    )
    unsupported = sorted({from_currency, to_currency} - supported)
    if unsupported:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"unsupported currency: {', '.join(unsupported)}",
        )

    rates, dates = latest_rates(db)
    try:
        converted = convert(amount, from_currency, to_currency, rates)
        rate = cross_rate(from_currency, to_currency, rates)
    except MissingRateError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return ConversionOut(
        amount=amount,
        from_currency=from_currency,
        to_currency=to_currency,
        converted=converted,
        rate=rate,
        rates_as_of={
            code: dates[code] for code in (from_currency, to_currency) if code in dates
        },
    )


@router.get("/latest", response_model=list[RateOut])
def list_latest_rates(db: DbSession) -> list[RateOut]:
    rates, dates = latest_rates(db)
    return [
        RateOut(currency=c, rate_to_usd=r, rate_date=dates[c]) for c, r in rates.items()
    ]


@router.post("/refresh", response_model=RefreshOut)
def refresh_rates(db: DbSession, force: bool = False) -> RefreshOut:
    """Pull live rates from the provider, at most once per REFRESH_INTERVAL unless forced.

    Safe to call on every dashboard load. On failure, stored rates are left untouched.
    """
    result = refresh_exchange_rates(db, force=force)
    if not result.ok:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"exchange-rate provider unavailable: {result.error}",
        )
    db.commit()
    return RefreshOut(
        rate_date=result.rate_date,
        skipped=result.skipped,
        stored=result.stored,
        missing=result.missing,
    )
