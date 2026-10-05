from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Country, Currency
from app.seed import iso_data
from app.seed.reference import check_reference_data, load_reference_data

TABLES = (Currency, Country)


def counts(db: Session) -> dict[str, int]:
    return {
        t.__tablename__: db.scalar(select(func.count()).select_from(t)) for t in TABLES
    }


def test_loaded_reference_data_passes_smoke_check(db):
    load_reference_data(db)

    assert check_reference_data(db) == []


def test_every_country_and_currency_is_present(db):
    load_reference_data(db)

    assert set(iso_data.currencies()) <= set(db.scalars(select(Currency.code)))
    assert set(iso_data.countries()) <= set(db.scalars(select(Country.code)))


def test_loading_twice_inserts_nothing_new(db):
    load_reference_data(db)
    first = counts(db)

    load_reference_data(db)

    assert counts(db) == first
