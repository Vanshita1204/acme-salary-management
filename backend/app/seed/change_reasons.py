"""The shared list of change reasons any compensation record can carry.

Usage (from backend/):
    python -m app.seed.change_reasons

Only inserts what's missing — edit REASONS to add one, then re-run. HR can also add
reasons from the UI (Phase 1.3 / 13).
"""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import ChangeReason

# Codes the business rules look up by name, so they must always exist:
#   new_hire   — required on an employee's initial base-pay record (FR-2/FR-5)
#   relocation — written by a country change (FR-7)
#   correction — fixes a mistake; excluded from average-increase stats (§5)
REQUIRED_CODES = ("new_hire", "relocation", "correction")

# (code, label)
REASONS: list[tuple[str, str]] = [
    ("new_hire", "New hire"),
    ("annual_revision", "Annual revision"),
    ("promotion", "Promotion"),
    ("market_adjustment", "Market adjustment"),
    ("role_change", "Role change"),
    ("relocation", "Relocation"),
    ("bonus_payout", "Bonus payout"),
    ("equity_grant", "Equity grant"),
    ("retention_award", "Retention award"),
    ("policy_change", "Policy change"),
    ("correction", "Correction"),
]


def load_change_reasons(session: Session) -> None:
    session.execute(
        insert(ChangeReason)
        .values([{"code": code, "label": label} for code, label in REASONS])
        .on_conflict_do_nothing(index_elements=["code"])
    )


def check_change_reasons(session: Session) -> list[str]:
    """Smoke check: every reason the business rules depend on exists."""
    present = set(
        session.scalars(
            select(ChangeReason.code).where(ChangeReason.code.in_(REQUIRED_CODES))
        )
    )
    return [
        f"required change reason {code!r} is missing"
        for code in REQUIRED_CODES
        if code not in present
    ]


def main() -> None:
    with SessionLocal() as session, session.begin():
        load_change_reasons(session)
        problems = check_change_reasons(session)
        if problems:
            # Raising inside session.begin() rolls the load back.
            raise SystemExit("Change-reason check failed:\n  " + "\n  ".join(problems))
        count = session.scalar(select(func.count()).select_from(ChangeReason))
    print(f"Change reasons ready: {count}.")


if __name__ == "__main__":
    main()
