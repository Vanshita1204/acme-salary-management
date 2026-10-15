from sqlalchemy import select

from app.models import Company
from app.seed.companies import SUBSIDIARIES, company_names, load_companies
from app.seed.employees import COUNTRIES


def test_every_seeded_country_belongs_to_exactly_one_entity():
    served = [c for countries in SUBSIDIARIES.values() for c in countries]

    assert sorted(served) == sorted(COUNTRIES)


def test_names_are_fixed_and_unique():
    assert company_names() == company_names()
    assert len(set(company_names())) == len(SUBSIDIARIES)
    assert company_names(3) == company_names()[:3]


def test_loads_every_entity(db):
    load_companies(db)

    assert set(company_names()) <= set(db.scalars(select(Company.name)))


def test_reloading_inserts_nothing(db):
    load_companies(db)

    assert load_companies(db) == 0
