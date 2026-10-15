"""ACME's legal entities: a fixed set of named subsidiaries, each serving the countries
it employs in. The employee seed places people in the entity that serves their country.

Usage (from backend/):
    python -m app.seed.companies
"""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Company

# entity name -> the countries it employs in (every seeded country appears exactly once)
SUBSIDIARIES: dict[str, tuple[str, ...]] = {
    "Acme Inc.": ("US", "CA"),
    "Acme India Pvt Ltd": ("IN",),
    "Acme UK Ltd": ("GB", "IE"),
    "Acme GmbH": ("DE",),
    "Acme Europe B.V.": ("NL", "FR", "ES", "PL", "SE"),
    "Acme Latin America Ltda": ("BR", "MX"),
    "Acme APAC Pte Ltd": ("SG", "AU"),
    "Acme Middle East FZ-LLC": ("AE",),
}


def company_names(count: int | None = None) -> list[str]:
    """The first `count` entities (all by default), in a fixed order."""
    return list(SUBSIDIARIES)[:count]


def load_companies(session: Session, count: int | None = None) -> int:
    """Insert entities that don't exist yet; returns how many were inserted."""
    result = session.execute(
        insert(Company)
        .values([{"name": name} for name in company_names(count)])
        .on_conflict_do_nothing(index_elements=["name"])
        .returning(Company.id)
    )
    return len(result.all())


def main() -> None:
    with SessionLocal() as session, session.begin():
        inserted = load_companies(session)
    print(f"Inserted {inserted} of {len(SUBSIDIARIES)} companies (others already existed).")


if __name__ == "__main__":
    main()
