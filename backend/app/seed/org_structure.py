"""Departments, job titles and job levels (Phase 7A).

Usage (from backend/):
    python -m app.seed.org_structure

Only inserts what's missing — edit the lists below and re-run. HR can also add entries
from the UI. The employee seed (Phase 3) draws from exactly these lists.
"""

import re

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Department, JobLevel, JobTitle

# department -> the titles the employee seed hires into it. Titles themselves are
# universal (not tied to a department); this pairing is only for generating data.
DEPARTMENT_TITLES: dict[str, list[str]] = {
    "Engineering": [
        "Software Engineer",
        "QA Engineer",
        "DevOps Engineer",
        "Data Engineer",
    ],
    "Product": ["Product Manager", "Product Designer"],
    "Sales": ["Account Executive", "Sales Development Representative"],
    "Marketing": ["Marketing Manager", "Content Strategist"],
    "Finance": ["Financial Analyst", "Accountant"],
    "People": ["People Partner", "Recruiter"],
    "Operations": ["Operations Analyst", "Office Manager"],
    "Customer Support": ["Support Specialist", "Support Team Lead"],
}
JOB_TITLES: list[str] = [t for titles in DEPARTMENT_TITLES.values() for t in titles]
LEVEL_CODES: list[str] = [f"L{n}" for n in range(1, 8)]


def level_label(code: str) -> str:
    """'L3' -> 'Level 3'; anything else is its own label. Migration 0011 uses the
    same rule when backfilling levels from existing employees."""
    match = re.fullmatch(r"[Ll](\d+)", code)
    return f"Level {match.group(1)}" if match else code


def level_rank(code: str) -> int:
    return int(re.sub(r"\D", "", code))


def load_org_structure(session: Session) -> None:
    session.execute(
        insert(Department)
        .values([{"name": name} for name in DEPARTMENT_TITLES])
        .on_conflict_do_nothing(index_elements=["name"])
    )
    session.execute(
        insert(JobTitle)
        .values([{"name": name} for name in JOB_TITLES])
        .on_conflict_do_nothing(index_elements=["name"])
    )
    # No conflict target: skip a level whose code *or* rank is already taken.
    session.execute(
        insert(JobLevel)
        .values(
            [
                {"code": code, "label": level_label(code), "rank": level_rank(code)}
                for code in LEVEL_CODES
            ]
        )
        .on_conflict_do_nothing()
    )


def check_org_structure(session: Session) -> list[str]:
    """Smoke check: everything the employee seed hires into exists."""
    departments = set(session.scalars(select(func.lower(Department.name))))
    titles = set(session.scalars(select(func.lower(JobTitle.name))))
    levels = set(session.scalars(select(func.lower(JobLevel.code))))
    return (
        [
            f"department {n!r} is missing"
            for n in DEPARTMENT_TITLES
            if n.lower() not in departments
        ]
        + [f"job title {n!r} is missing" for n in JOB_TITLES if n.lower() not in titles]
        + [
            f"job level {c!r} is missing"
            for c in LEVEL_CODES
            if c.lower() not in levels
        ]
    )


def main() -> None:
    with SessionLocal() as session, session.begin():
        load_org_structure(session)
        problems = check_org_structure(session)
        if problems:
            # Raising inside session.begin() rolls the load back.
            raise SystemExit("Org-structure check failed:\n  " + "\n  ".join(problems))
        counts = [
            session.scalar(select(func.count()).select_from(model))
            for model in (Department, JobTitle, JobLevel)
        ]
    print(
        f"Org structure ready: {counts[0]} departments, {counts[1]} job titles, "
        f"{counts[2]} job levels."
    )


if __name__ == "__main__":
    main()
