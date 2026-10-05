"""Synthetic company rows, generated with Faker.

Usage (from backend/):
    python -m app.seed.companies [--count 100] [--seed 1204]

Deterministic: the same seed and count (and the pinned Faker version) always produce the
same names, so re-running inserts nothing new.
"""

import argparse

from faker import Faker
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Company

DEFAULT_COUNT = 100
DEFAULT_SEED = 1204


def company_names(count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED) -> list[str]:
    fake = Faker("en_US")
    fake.seed_instance(seed)
    return [fake.unique.company() for _ in range(count)]


def load_companies(
    session: Session, count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED
) -> int:
    """Insert generated companies that don't exist yet; returns how many were inserted."""
    result = session.execute(
        insert(Company)
        .values([{"name": name} for name in company_names(count, seed)])
        .on_conflict_do_nothing(index_elements=["name"])
        .returning(Company.id)
    )
    return len(result.all())


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic companies.")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    with SessionLocal() as session, session.begin():
        inserted = load_companies(session, args.count, args.seed)
    print(f"Inserted {inserted} of {args.count} companies (others already existed).")


if __name__ == "__main__":
    main()
