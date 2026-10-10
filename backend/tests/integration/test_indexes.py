"""Indexes behind the 500 ms target (SPECIFICATION §7): "indexes on every filtered column".

Checked against the schema Alembic actually built, and tied to the code that builds the
directory's filters, so adding a filter without an index fails here.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.sql import visitors

from app.models import Employee
from app.services.directory import DirectoryQuery, apply_filters

# Matched by text search (ILIKE), which no B-tree can serve; covered by Scale Lab E4.
SEARCHED = {"first_name", "last_name", "email", "code"}


def leading_columns(db, table: str) -> set[str]:
    """Columns that start some index on the table (expression indexes excluded)."""
    rows = db.execute(
        text(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0]
            WHERE c.relname = :table AND i.indkey[0] <> 0
            """
        ),
        {"table": table},
    )
    return {name for (name,) in rows}


def index_definitions(db, table: str) -> list[str]:
    return [
        row[0]
        for row in db.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = :table"),
            {"table": table},
        )
    ]


def filtered_employee_columns() -> set[str]:
    """Every employees column the directory's filters can put in a WHERE clause."""
    everything = DirectoryQuery(
        q="x",
        department_ids=[1],
        countries=["IN"],
        job_title_ids=[1],
        job_level_ids=[1],
        min_level_rank=1,
        max_level_rank=9,
        statuses=["active"],
    )
    stmt = apply_filters(select(Employee.id), everything)
    columns: set[str] = set()
    visitors.traverse(
        stmt.whereclause,
        {},
        {
            "column": lambda column: (
                columns.add(column.name)
                if getattr(column.table, "name", "") == "employees"
                else None
            )
        },
    )
    return columns


def test_the_filters_use_the_columns_we_expect():
    assert filtered_employee_columns() >= {
        "department_id",
        "current_country",
        "job_title_id",
        "job_level_id",
        "status",
    }


def test_every_filtered_column_leads_an_index(db):
    indexed = leading_columns(db, "employees")
    missing = filtered_employee_columns() - SEARCHED - indexed
    assert not missing, f"filtered without an index: {sorted(missing)}"


@pytest.mark.parametrize(
    "column",
    [
        "company_id",
        "department_id",
        "job_title_id",
        "job_level_id",
        "current_country",
        "status",
    ],
)
def test_each_employee_dimension_has_its_own_index(db, column):
    assert column in leading_columns(db, "employees")


def test_the_keyset_sorts_have_matching_indexes(db):
    definitions = " ".join(index_definitions(db, "employees"))
    assert (
        "lower(last_name)" in definitions and "lower(first_name)" in definitions
    )  # sort by name
    assert "(hire_date, id)" in definitions  # sort by hire date


def test_history_lookups_are_indexed(db):
    definitions = index_definitions(db, "compensation_records")
    assert any(
        "(employee_id, compensation_type_id, effective_date DESC)" in d
        for d in definitions
    )
    assert any(
        "ix_comp_records_future_dated" in d and "WHERE" in d for d in definitions
    )  # promotion
    assert "compensation_type_id" in leading_columns(db, "current_compensation")
    assert any(
        "(currency, rate_date DESC)" in d
        for d in index_definitions(db, "exchange_rates")
    )
