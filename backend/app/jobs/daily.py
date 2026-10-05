"""The daily job: refresh exchange rates (FR-8) and promote due compensation (FR-4).

Usage (from backend/):
    python -m app.jobs.daily            # run once, e.g. from cron at 00:10 UTC
    python -m app.jobs.daily --forever  # long-running worker: run now, then daily

Each step commits on its own, so a provider outage never blocks promotion. A failed
rate fetch keeps serving the stored rates (every response shows their date) and makes
the run exit non-zero, so cron or the container's restart policy surfaces it.

Both steps are safe to repeat: the refresh is throttled (`MIN_REFETCH_INTERVAL`) and
promotion only moves pointers that are still behind.
"""

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.services.compensation import promote_due_records
from app.services.exchange_rates import RefreshResult, refresh_exchange_rates

logger = logging.getLogger(__name__)

# When the worker wakes: the provider publishes shortly after 00:00 UTC.
RUN_AT = (0, 10)  # hour, minute, UTC
# The job is the scheduler, so it doesn't wait out the API's 24 h throttle (a run at
# 00:10 would otherwise skip because yesterday's fetch was at 00:10:02). This only
# stops a crash-looping worker from hammering the provider.
MIN_REFETCH_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True)
class DailyResult:
    rates: RefreshResult
    promoted: int  # current-compensation rows moved to a now-due record

    @property
    def ok(self) -> bool:
        return self.rates.ok


def run_daily(
    session: Session, client: httpx.Client | None = None, today: date | None = None
) -> DailyResult:
    rates = refresh_exchange_rates(session, client, max_age=MIN_REFETCH_INTERVAL)
    session.commit()
    promoted = promote_due_records(session, today)
    session.commit()
    return DailyResult(rates=rates, promoted=promoted)


def report(result: DailyResult) -> None:
    rates = result.rates
    if not rates.ok:
        logger.error(
            "exchange rates: refresh failed (%s); stored rates kept", rates.error
        )
    elif rates.skipped:
        logger.info("exchange rates: fetched within the last hour; skipped")
    else:
        logger.info("exchange rates: stored %d for %s", rates.stored, rates.rate_date)
    logger.info("compensation: promoted %d due record(s)", result.promoted)


def run_once() -> DailyResult:
    with SessionLocal() as session:
        result = run_daily(session)
    report(result)
    return result


def seconds_until_next_run(now: datetime) -> float:
    hour, minute = RUN_AT
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


def run_forever() -> None:
    while True:
        try:
            run_once()
        except Exception:  # keep the worker alive; the next run retries
            logger.exception("daily job failed")
        delay = seconds_until_next_run(datetime.now(UTC))
        logger.info("next run in %.0f s", delay)
        time.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--forever", action="store_true", help="run now, then every day at 00:10 UTC"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.forever:
        run_forever()
    elif not run_once().ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
