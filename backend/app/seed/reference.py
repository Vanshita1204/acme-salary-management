"""Idempotent loader for reference data (Phase 1.2): currencies and countries.

Usage (from backend/, after `alembic upgrade head`):
    python -m app.seed.reference

Also pulls live exchange rates once the reference rows are in (non-fatal if the provider
is down).

Only inserts rows that are missing — never updates or deletes — so it's safe to re-run.
"""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Country, Currency
from app.seed import iso_data
from app.services.exchange_rates import refresh_exchange_rates


def load_reference_data(session: Session) -> None:
    session.execute(
        insert(Currency)
        .values(
            [
                {"code": code, "name": name}
                for code, name in iso_data.currencies().items()
            ]
        )
        .on_conflict_do_nothing(index_elements=["code"])
    )
    session.execute(
        insert(Country)
        .values(
            [
                {"code": code, "name": name, "default_currency": currency}
                for code, (name, currency) in iso_data.countries().items()
            ]
        )
        .on_conflict_do_nothing(index_elements=["code"])
    )


def check_reference_data(session: Session) -> list[str]:
    """Smoke check: returns a list of problems, empty when the reference data is usable."""
    problems: list[str] = []

    for table in (Currency, Country):
        if not session.scalar(select(func.count()).select_from(table)):
            problems.append(f"{table.__tablename__} is empty")

    unresolved = session.scalars(
        select(Country.code)
        .outerjoin(Currency, Currency.code == Country.default_currency)
        .where(Currency.code.is_(None))
    ).all()
    problems += [
        f"country {code}: default_currency does not resolve" for code in unresolved
    ]

    return problems


def main() -> None:
    with SessionLocal() as session, session.begin():
        load_reference_data(session)
        problems = check_reference_data(session)
        if problems:
            # Raising inside session.begin() rolls the load back.
            raise SystemExit("Reference data check failed:\n  " + "\n  ".join(problems))
    print("Reference data loaded and verified.")

    # Live rates are fetched, not seeded; a provider outage shouldn't fail the reference load.
    with SessionLocal() as session, session.begin():
        result = refresh_exchange_rates(session)
    if result.skipped:
        print(f"Exchange rates for {result.rate_date} are still fresh; not re-fetched.")
    elif result.ok:
        print(f"Stored {result.stored} live exchange rates for {result.rate_date}.")
        if result.missing:
            print(f"No provider rate for: {', '.join(result.missing)}")
    else:
        print(
            f"WARNING: exchange rates not refreshed ({result.error}); run "
            "`python -m app.services.exchange_rates` once the provider is reachable."
        )


if __name__ == "__main__":
    main()
