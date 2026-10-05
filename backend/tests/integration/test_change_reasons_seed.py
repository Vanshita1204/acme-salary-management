from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import ChangeReason
from app.seed.change_reasons import (
    REASONS,
    REQUIRED_CODES,
    check_change_reasons,
    load_change_reasons,
)


def count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ChangeReason))


def test_loaded_reasons_pass_smoke_check(db):
    load_change_reasons(db)

    assert check_change_reasons(db) == []


def test_every_reason_is_present(db):
    load_change_reasons(db)

    assert {code for code, _ in REASONS} <= set(db.scalars(select(ChangeReason.code)))


def test_seed_list_contains_every_required_code():
    assert set(REQUIRED_CODES) <= {code for code, _ in REASONS}


def test_loading_twice_inserts_nothing_new(db):
    load_change_reasons(db)
    first = count(db)

    load_change_reasons(db)

    assert count(db) == first


def test_smoke_check_flags_missing_required_reason(db):
    load_change_reasons(db)
    db.execute(delete(ChangeReason).where(ChangeReason.code == "relocation"))

    assert check_change_reasons(db) == [
        "required change reason 'relocation' is missing"
    ]
