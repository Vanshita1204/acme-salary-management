from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Department, JobLevel, JobTitle
from app.seed.org_structure import (
    DEPARTMENT_TITLES,
    JOB_TITLES,
    LEVEL_CODES,
    check_org_structure,
    level_label,
    load_org_structure,
)


def counts(db: Session) -> tuple[int, int, int]:
    return tuple(
        db.scalar(select(func.count()).select_from(model))
        for model in (Department, JobTitle, JobLevel)
    )


def test_loaded_org_structure_passes_smoke_check(db):
    load_org_structure(db)

    assert check_org_structure(db) == []


def test_levels_are_ranked_by_number(db):
    load_org_structure(db)

    levels = db.execute(
        select(JobLevel.code, JobLevel.label, JobLevel.rank).where(
            JobLevel.code.in_(LEVEL_CODES)
        )
    ).all()
    assert sorted(levels, key=lambda row: row.rank) == [
        (code, level_label(code), n) for n, code in enumerate(LEVEL_CODES, start=1)
    ]


def test_loading_twice_inserts_nothing_new(db):
    load_org_structure(db)
    first = counts(db)

    load_org_structure(db)

    assert counts(db) == first


def test_titles_are_unique_across_departments():
    assert len(set(JOB_TITLES)) == len(JOB_TITLES)
    assert all(DEPARTMENT_TITLES.values())


def test_smoke_check_flags_missing_entries(db):
    load_org_structure(db)
    db.query(JobLevel).filter(JobLevel.code == "L7").update({"code": "X7"})

    assert check_org_structure(db) == ["job level 'L7' is missing"]
