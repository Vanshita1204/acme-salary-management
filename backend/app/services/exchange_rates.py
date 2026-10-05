"""Live exchange rates: fetch from the provider, store, and read back the latest.

Requests never call the provider (§7 Resilience) — conversions and dashboards read stored
rates, and `refresh_exchange_rates` is what pulls fresh ones in. The provider publishes
once a day, so a refresh within REFRESH_INTERVAL of the last fetch is a no-op unless
forced. Run it manually with:
    python -m app.services.exchange_rates [--force]
"""

import argparse
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.domain.currency import rates_to_usd_from_usd_base
from app.models import Currency, ExchangeRate

logger = logging.getLogger(__name__)

# The provider updates daily; fetching more often just burns quota for identical data.
REFRESH_INTERVAL = timedelta(hours=24)


class ExchangeRateProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchedRates:
    rate_date: date
    rates_to_usd: dict[str, Decimal]
    source: str


@dataclass(frozen=True)
class RefreshResult:
    ok: bool
    rate_date: date | None = None
    stored: int = 0
    # Supported currencies the provider lacks.
    missing: list[str] = field(default_factory=list)
    error: str | None = None
    # True when stored rates were fresh enough that the provider wasn't called.
    skipped: bool = False


def parse_provider_response(payload: dict, source: str) -> FetchedRates:
    """Parse an open.er-api.com-style `latest/USD` response."""
    if payload.get("result") != "success":
        raise ExchangeRateProviderError(f"provider returned {payload.get('result')!r}")
    if payload.get("base_code") != "USD":
        raise ExchangeRateProviderError(
            f"expected USD base, got {payload.get('base_code')!r}"
        )
    try:
        updated = datetime.fromtimestamp(payload["time_last_update_unix"], tz=UTC)
        rates = rates_to_usd_from_usd_base(payload["rates"])
    except (KeyError, TypeError, AttributeError, ArithmeticError) as exc:
        raise ExchangeRateProviderError(
            f"malformed provider response: {exc!r}"
        ) from exc
    return FetchedRates(rate_date=updated.date(), rates_to_usd=rates, source=source)


def fetch_live_rates(client: httpx.Client, url: str) -> FetchedRates:
    try:
        response = client.get(url, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ExchangeRateProviderError(f"fetch failed: {exc}") from exc
    return parse_provider_response(payload, source=urlparse(url).netloc)


def store_rates(session: Session, fetched: FetchedRates) -> tuple[int, list[str]]:
    """Upsert one row per supported currency for the fetched date.

    Returns (rows stored, supported currencies the provider had no rate for).
    """
    supported = set(session.scalars(select(Currency.code)))
    rows = [
        {
            "currency": code,
            "rate_to_usd": rate,
            "rate_date": fetched.rate_date,
            "source": fetched.source,
        }
        for code, rate in fetched.rates_to_usd.items()
        if code in supported
    ]
    if rows:
        stmt = insert(ExchangeRate).values(rows)
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_exchange_rates_currency_date",
                set_={
                    "rate_to_usd": stmt.excluded.rate_to_usd,
                    "source": stmt.excluded.source,
                    "fetched_at": func.now(),
                },
            )
        )
    return len(rows), sorted(supported - fetched.rates_to_usd.keys())


def last_fetched_at(session: Session) -> datetime | None:
    return session.scalar(select(func.max(ExchangeRate.fetched_at)))


def refresh_exchange_rates(
    session: Session, client: httpx.Client | None = None, *, force: bool = False
) -> RefreshResult:
    """Fetch and store live rates, unless the stored ones are newer than REFRESH_INTERVAL.

    On provider failure, logs and leaves stored rates as-is.
    """
    if not force:
        fetched_at = last_fetched_at(session)
        fresh_cutoff = session.scalar(select(func.now())) - REFRESH_INTERVAL
        if fetched_at is not None and fetched_at > fresh_cutoff:
            latest_date = session.scalar(select(func.max(ExchangeRate.rate_date)))
            return RefreshResult(ok=True, rate_date=latest_date, skipped=True)

    url = get_settings().exchange_rate_api_url
    try:
        if client is None:
            with httpx.Client() as own_client:
                fetched = fetch_live_rates(own_client, url)
        else:
            fetched = fetch_live_rates(client, url)
    except ExchangeRateProviderError as exc:
        logger.warning(
            "Exchange-rate refresh failed; keeping latest stored rates: %s", exc
        )
        return RefreshResult(ok=False, error=str(exc))

    stored, missing = store_rates(session, fetched)
    if missing:
        logger.warning(
            "Provider has no rate for supported currencies: %s", ", ".join(missing)
        )
    return RefreshResult(
        ok=True, rate_date=fetched.rate_date, stored=stored, missing=missing
    )


def latest_rates(session: Session) -> tuple[dict[str, Decimal], dict[str, date]]:
    """Most recent stored rate per currency, and the date each of those rates is from."""
    rows = session.execute(
        select(ExchangeRate.currency, ExchangeRate.rate_to_usd, ExchangeRate.rate_date)
        .ext(distinct_on(ExchangeRate.currency))
        .order_by(ExchangeRate.currency, ExchangeRate.rate_date.desc())
    ).all()
    return (
        {row.currency: row.rate_to_usd for row in rows},
        {row.currency: row.rate_date for row in rows},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch live exchange rates.")
    parser.add_argument(
        "--force", action="store_true", help="fetch even if stored rates are fresh"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with SessionLocal() as session, session.begin():
        result = refresh_exchange_rates(session, force=args.force)
    if not result.ok:
        raise SystemExit(f"Refresh failed: {result.error}")
    if result.skipped:
        print(f"Rates for {result.rate_date} are still fresh; skipped (use --force).")
    else:
        print(f"Stored {result.stored} rates for {result.rate_date}.")


if __name__ == "__main__":
    main()
