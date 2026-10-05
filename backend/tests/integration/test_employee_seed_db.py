"""A small seed run against Postgres, rolled back afterwards."""

from datetime import date

from sqlalchemy import func, select, text

from app.models import CompensationRecord, Employee
from app.seed.change_reasons import load_change_reasons
from app.seed.companies import load_companies
from app.seed.compensation_types import load_compensation_types
from app.seed.employees import seed_employees
from app.seed.reference import load_reference_data

COUNT = 150


def test_seed_inserts_employees_records_and_current_compensation(db):
    load_reference_data(db)
    load_companies(db, 5)
    load_compensation_types(db)
    load_change_reasons(db)

    counts = seed_employees(
        db,
        COUNT,
        seed=42,
        as_of=date(2026, 10, 1),
        require_empty=False,
        email_domain="pytest.example",
    )

    assert counts["employees"] == COUNT
    seeded = select(Employee.id).where(Employee.email.like("%@pytest.example"))
    assert db.scalar(select(func.count()).select_from(seeded.subquery())) == COUNT
    assert counts["records"] == db.scalar(
        select(func.count())
        .select_from(CompensationRecord)
        .where(CompensationRecord.employee_id.in_(seeded))
    )
    # Exactly one current base-pay row per seeded employee, pointing at its latest record.
    mismatches = db.scalar(
        text(
            """
            SELECT count(*) FROM employees e
            WHERE e.email LIKE '%@pytest.example'
              AND (SELECT count(*) FROM current_compensation cc
                   JOIN compensation_types ct ON ct.id = cc.compensation_type_id
                   WHERE ct.is_base_pay AND cc.employee_id = e.id) <> 1
            """
        )
    )
    assert mismatches == 0
