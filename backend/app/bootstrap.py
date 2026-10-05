"""First-run setup for a hosted instance (Phase 15): migrate, seed, ready to serve.

Usage (from backend/):
    python -m app.bootstrap [--employees 10000]

Every step is idempotent, so the container can run this on every start:
`alembic upgrade head`, the reference data (and a first live rate fetch), the
compensation catalog, change reasons, the org structure, then the synthetic employees —
only when there are none yet, because compensation history is append-only and can't be
reloaded. A rate-provider outage doesn't stop the app from starting; the daily job
retries it.
"""

import argparse
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models import Employee
from app.seed.change_reasons import check_change_reasons, load_change_reasons
from app.seed.companies import load_companies
from app.seed.compensation_types import (
    check_compensation_types,
    load_compensation_types,
)
from app.seed.employees import DEFAULT_COUNT, seed_employees
from app.seed.org_structure import check_org_structure, load_org_structure
from app.seed.reference import check_reference_data, load_reference_data
from app.services.exchange_rates import refresh_exchange_rates

BACKEND = Path(__file__).resolve().parent.parent


def migrate() -> None:
    command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")


def seed_reference_and_catalog() -> None:
    """Everything the app needs before it has a single employee."""
    steps = (
        (load_reference_data, check_reference_data),
        (load_compensation_types, check_compensation_types),
        (load_change_reasons, check_change_reasons),
        (load_org_structure, check_org_structure),
        (load_companies, lambda session: []),
    )
    with SessionLocal() as session, session.begin():
        for load, check in steps:
            load(session)
            session.flush()
            if problems := check(session):
                # Raising inside session.begin() rolls everything back.
                raise SystemExit(
                    f"{load.__name__} check failed: " + "; ".join(problems)
                )


def fetch_rates() -> str:
    try:
        with SessionLocal() as session, session.begin():
            result = refresh_exchange_rates(session)
    except Exception as exc:  # noqa: BLE001  the provider being down must not stop the app starting
        return (
            f"exchange rates not fetched ({type(exc).__name__}); the daily job retries"
        )
    if result.skipped:
        return f"exchange rates for {result.rate_date} are fresh"
    if result.ok:
        return f"stored {result.stored} exchange rates for {result.rate_date}"
    return f"exchange rates not fetched ({result.error}); the daily job retries"


def seed_population(count: int) -> str:
    with SessionLocal() as session:
        if session.scalar(select(func.count()).select_from(Employee)):
            return "employees already present; not reseeded"
        started = time.perf_counter()
        counts = seed_employees(session, count)
    return f"seeded {counts['employees']} employees in {time.perf_counter() - started:.0f}s"


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate and seed the database.")
    parser.add_argument("--employees", type=int, default=DEFAULT_COUNT)
    args = parser.parse_args()

    migrate()
    print("migrations applied", flush=True)
    seed_reference_and_catalog()
    print("reference data, catalog, reasons and org structure ready", flush=True)
    print(fetch_rates(), flush=True)
    print(seed_population(args.employees), flush=True)


if __name__ == "__main__":
    main()
