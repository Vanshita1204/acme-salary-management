from sqlalchemy import select

from app.models import Company
from app.seed.companies import company_names, load_companies


def test_names_are_deterministic_and_unique():
    names = company_names(100, seed=1204)

    assert names == company_names(100, seed=1204)
    assert len(set(names)) == 100
    assert names != company_names(100, seed=1)


def test_loads_every_generated_company(db):
    load_companies(db, 100, seed=1204)

    assert set(company_names(100, seed=1204)) <= set(db.scalars(select(Company.name)))


def test_reloading_inserts_nothing(db):
    load_companies(db, 100, seed=1204)

    assert load_companies(db, 100, seed=1204) == 0
