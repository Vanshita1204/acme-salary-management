from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CompensationType
from app.seed.compensation_types import (
    CATALOG,
    check_compensation_types,
    load_compensation_types,
)


def count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(CompensationType))


def test_loaded_catalog_passes_smoke_check(db):
    load_compensation_types(db)

    assert check_compensation_types(db) == []


def test_every_catalog_type_is_present(db):
    load_compensation_types(db)

    assert {spec.name for spec in CATALOG} <= set(
        db.scalars(select(CompensationType.name))
    )


def test_catalog_defines_exactly_one_base_pay_type():
    assert [spec.name for spec in CATALOG if spec.is_base_pay] == ["Base Pay"]


def test_loading_twice_inserts_nothing_new(db):
    load_compensation_types(db)
    first = count(db)

    load_compensation_types(db)

    assert count(db) == first
