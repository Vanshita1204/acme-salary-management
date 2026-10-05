"""Starting universal compensation-type catalog.

Usage (from backend/):
    python -m app.seed.compensation_types

Only inserts what's missing — edit CATALOG to add a type, then re-run. Change reasons
aren't tied to types; they're a separate shared list (`app.seed.change_reasons`).
"""

from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import CompensationType


class TypeSpec(NamedTuple):
    category: str
    subtype: str
    period_months: (
        int  # months one recorded amount covers: 1 monthly, 3 quarterly, 12 annual
    )
    is_base_pay: bool
    name: str


CATALOG: list[TypeSpec] = [
    TypeSpec("fixed", "base", 1, True, "Base Pay"),
    TypeSpec("variable", "annual_target", 12, False, "Annual Variable Pay (Target)"),
    TypeSpec("bonus", "quarterly", 3, False, "Quarterly Bonus"),
    TypeSpec("bonus", "annual", 12, False, "Annual Bonus"),
    TypeSpec("bonus", "retention", 12, False, "Retention Bonus"),
    TypeSpec("equity", "rsu", 12, False, "Annual Equity Grant (RSU)"),
    TypeSpec("allowance", "housing", 1, False, "Housing Allowance"),
    TypeSpec("allowance", "transport", 1, False, "Transport Allowance"),
    TypeSpec("allowance", "meal", 1, False, "Meal Allowance"),
    # Reimbursements: the amount is the employee's entitlement (cap) for the period,
    # not individual expense claims — those belong to an expenses system.
    TypeSpec(
        "reimbursement", "phone_internet", 1, False, "Phone & Internet Reimbursement"
    ),
    TypeSpec(
        "reimbursement", "learning", 12, False, "Learning & Development Reimbursement"
    ),
    TypeSpec("reimbursement", "wellness", 12, False, "Wellness Reimbursement"),
]


def load_compensation_types(session: Session) -> None:
    session.execute(
        insert(CompensationType)
        .values([spec._asdict() for spec in CATALOG])
        .on_conflict_do_nothing(constraint="uq_comp_types_category_subtype")
    )


def check_compensation_types(session: Session) -> list[str]:
    """Smoke check: returns a list of problems, empty when the catalog is usable."""
    base_pay_types = session.scalar(
        select(func.count())
        .select_from(CompensationType)
        .where(CompensationType.is_base_pay)
    )
    if base_pay_types != 1:
        return [f"expected exactly 1 base-pay type, found {base_pay_types}"]
    return []


def main() -> None:
    with SessionLocal() as session, session.begin():
        load_compensation_types(session)
        problems = check_compensation_types(session)
        if problems:
            # Raising inside session.begin() rolls the load back.
            raise SystemExit("Catalog check failed:\n  " + "\n  ".join(problems))
        types = session.scalar(select(func.count()).select_from(CompensationType))
    print(f"Compensation catalog ready: {types} types.")


if __name__ == "__main__":
    main()
