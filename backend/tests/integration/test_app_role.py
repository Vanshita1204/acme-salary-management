"""Compensation history can't be changed or removed: by the app's role, by the owner, or by
a mistake in the code. Defence in depth: privileges, row triggers, a TRUNCATE trigger."""

import csv
import io
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.db.grants import (
    SEQUENCES,
    TABLE_PRIVILEGES,
    apply_grants,
    create_role,
    grant_statements,
)
from app.db.session import Base
from app.models import ChangeReason, CompensationType, ExchangeRate
from app.seed.change_reasons import load_change_reasons
from app.seed.compensation_types import load_compensation_types
from app.seed.reference import load_reference_data
from app.services.compensation import utc_today
from app.services.exchange_rates import refresh_exchange_rates


def denied(db, statement: str) -> str:
    """Run a statement that must fail; returns the database's message."""
    with (
        pytest.raises((ProgrammingError, DBAPIError)) as caught,
        db.begin_nested(),
    ):  # a failure only undoes this statement, not the whole test
        db.execute(text(statement))
    return str(caught.value)


# --- the owner is stopped by the triggers ---


@pytest.fixture
def some_history(db, client, org):
    """At least one record to try to change. (On an empty table UPDATE and DELETE touch no
    rows, so no row trigger runs and nothing is refused: the test would prove nothing.)"""
    load_reference_data(db)
    load_compensation_types(db)
    load_change_reasons(db)
    company = client.post(
        "/companies", json={"name": f"History {uuid.uuid4().hex[:8]}"}
    ).json()
    response = client.post(
        "/employees",
        json={
            "company_id": company["id"],
            "first_name": "Hi",
            "last_name": "Story",
            "email": f"{uuid.uuid4().hex[:10]}@history.example",
            **org["role"],
            "current_country": "IN",
            "currency": "INR",
            "hire_date": "2024-01-15",
            "base_pay": {"amount": "1000"},
            "changed_by": "pytest",
        },
    )
    assert response.status_code == 201, response.text
    assert db.scalar(text("SELECT count(*) FROM compensation_records")) >= 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE compensation_records SET amount = amount + 1",
        "DELETE FROM compensation_records",
        "TRUNCATE compensation_records CASCADE",
        "TRUNCATE employees CASCADE",  # reaches the history through the foreign key
    ],
)
def test_even_the_owner_cannot_change_or_remove_history(db, some_history, statement):
    assert "append-only" in denied(db, statement)


def test_plain_truncate_is_refused_too(db):
    # Postgres itself refuses (other tables reference the history); either reason is fine.
    message = denied(db, "TRUNCATE compensation_records")
    assert "append-only" in message or "referenced in a foreign key" in message


def test_the_guarding_triggers_are_all_enabled(db):
    rows = db.execute(
        text(
            "SELECT tgname, tgenabled FROM pg_trigger WHERE tgrelid = 'compensation_records'::regclass AND NOT tgisinternal"
        )
    ).all()
    assert {name: state for name, state in rows} == {
        "trg_validate_compensation_record": "O",
        "trg_comp_records_no_update": "O",
        "trg_comp_records_no_truncate": "O",
    }


# --- the application's role can't even try ---


@pytest.fixture
def app_role(db):
    """A role with exactly the application's privileges (made inside the test's transaction)."""
    role = f"acme_test_{uuid.uuid4().hex[:8]}"
    connection = db.connection()
    try:
        with db.begin_nested():
            create_role(connection, role, None, login=False)
    except (ProgrammingError, DBAPIError) as error:
        pytest.skip(f"needs a database user that can create roles: {error.orig}")
    apply_grants(connection, role)
    return role


def become(db, role: str) -> None:
    db.execute(text(f"SET LOCAL ROLE {role}"))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE compensation_records SET amount = amount + 1",
        "DELETE FROM compensation_records",
        "TRUNCATE compensation_records CASCADE",
        "TRUNCATE employees CASCADE",
        "ALTER TABLE compensation_records DISABLE TRIGGER trg_comp_records_no_update",
        "DROP TRIGGER trg_comp_records_no_update ON compensation_records",
        "DROP TABLE compensation_records CASCADE",
    ],
)
def test_the_app_role_is_refused_before_the_triggers_are_reached(
    db, app_role, statement
):
    become(db, app_role)
    message = denied(db, statement)
    assert "permission denied" in message or "must be owner" in message, message


def test_the_app_role_can_read_history(db, app_role):
    become(db, app_role)
    assert db.scalar(text("SELECT count(*) FROM compensation_records")) >= 0


def test_the_app_role_may_never_delete_anything(db, app_role):
    become(db, app_role)
    for table in TABLE_PRIVILEGES:
        message = denied(db, f"DELETE FROM {table}")
        assert "permission denied" in message, f"{table}: {message}"


class FakeProvider(httpx.Client):
    def __init__(self):
        payload = {
            "result": "success",
            "base_code": "USD",
            "time_last_update_unix": 4071081600,
            "rates": {"USD": 1, "EUR": 0.5, "INR": 100},
        }
        super().__init__(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
        )


def test_the_whole_application_works_as_the_app_role(db, client, org, app_role):
    """If a grant were missing, some part of this walk through the product would fail."""
    load_reference_data(db)
    load_compensation_types(db)
    load_change_reasons(db)
    types = {
        (t.category, t.subtype): t.id for t in db.scalars(select(CompensationType))
    }
    reasons = {r.code: r.id for r in db.scalars(select(ChangeReason))}
    db.flush()
    become(db, app_role)  # from here on, only what the running app is allowed to do

    def ok(response, status=200):
        assert response.status_code == status, response.text
        return response.json()

    tag = uuid.uuid4().hex[:8]
    company = ok(client.post("/companies", json={"name": f"Role test {tag}"}), 201)
    ok(client.post("/departments", json={"name": f"Dept {tag}"}), 201)
    level = ok(
        client.post(
            "/job-levels",
            json={
                "code": f"R{tag}",
                "label": "r",
                "rank": 7_000_000 + uuid.uuid4().int % 10**6,
            },
        ),
        201,
    )
    ok(client.patch(f"/job-levels/{level['id']}", json={"label": "renamed"}))
    ok(
        client.post("/change-reasons", json={"code": f"r_{tag}", "label": "Role test"}),
        201,
    )
    ok(
        client.post(
            "/compensation-types",
            json={"name": f"Type {tag}", "category": f"c{tag}", "period_months": 1},
        ),
        201,
    )

    employee = ok(
        client.post(
            "/employees",
            json={
                "company_id": company["id"],
                "first_name": "Rita",
                "last_name": tag,
                "email": f"rita.{tag}@role.example",
                **org["role"],
                "current_country": "IN",
                "currency": "INR",
                "hire_date": "2024-01-15",
                "base_pay": {"amount": "200000"},
                "changed_by": "pytest",
            },
        ),
        201,
    )
    eid = employee["id"]

    def change(**fields):
        body = {
            "compensation_type_id": types[("fixed", "base")],
            "change_reason_id": reasons["annual_revision"],
            "effective_date": "2025-04-01",
            "amount": "220000",
            "changed_by": "pytest",
        }
        return ok(
            client.post(f"/employees/{eid}/compensation", json=body | fields), 201
        )

    change()
    change(
        change_reason_id=reasons["correction"],
        amount="221000",
        effective_date="2025-05-01",
    )
    change(
        effective_date=str(utc_today() + timedelta(days=30)), amount="250000"
    )  # future-dated
    ok(
        client.get(f"/employees/{eid}")
    )  # promotes due records (an upsert on current_compensation)
    ok(client.patch(f"/employees/{eid}", json={"first_name": "Rita-Ann"}))
    ok(
        client.post(
            f"/employees/{eid}/relocate",
            json={
                "country": "DE",
                "effective_date": str(utc_today()),
                "changed_by": "pytest",
            },
        )
    )
    ok(client.get("/employees", params={"q": tag}))
    export = client.get("/employees/export", params={"q": tag})
    assert export.status_code == 200 and tag in export.text
    for view in ("summary", "outliers", "composition", "histogram"):
        ok(client.get(f"/analytics/{view}", params={"q": tag}))
    ok(
        client.get(
            "/analytics/changes",
            params={"q": tag, "date_from": "2025-01-01", "date_to": str(utc_today())},
        )
    )

    other = ok(
        client.post(
            "/employees",
            json={
                "company_id": company["id"],
                "first_name": "Cur",
                "last_name": tag,
                "email": f"cur.{tag}@role.example",
                **org["role"],
                "current_country": "IN",
                "currency": "INR",
                "hire_date": "2024-01-15",
                "base_pay": {"amount": "100000"},
                "changed_by": "pytest",
            },
        ),
        201,
    )
    ok(
        client.post(
            f"/employees/{other['id']}/change-currency",
            json={
                "currency": "EUR",
                "amounts": [
                    {"compensation_type_id": types[("fixed", "base")], "amount": "1200"}
                ],
                "change_reason_id": reasons["market_adjustment"],
                "effective_date": str(utc_today()),
                "changed_by": "pytest",
            },
        )
    )
    ok(
        client.post(
            f"/employees/{other['id']}/terminate",
            json={"termination_date": str(utc_today())},
        )
    )

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "first_name",
            "last_name",
            "email",
            "company",
            "department",
            "job_title",
            "job_level",
            "country",
            "hire_date",
            "currency",
            "base_pay_amount",
        ]
    )
    writer.writerow(
        [
            "Imp",
            tag,
            f"imp.{tag}@role.example",
            f"Role test {tag}",
            "Engineering",
            "Software Engineer",
            "L3",
            "IN",
            "2024-01-15",
            "INR",
            "150000",
        ]
    )
    files = {"file": ("people.csv", out.getvalue().encode(), "text/csv")}
    ok(client.post("/import/validate", files=files))
    ok(
        client.post(
            "/import/confirm",
            files={"file": ("people.csv", out.getvalue().encode(), "text/csv")},
            data={"changed_by": "pytest"},
        ),
        201,
    )

    assert refresh_exchange_rates(
        db, FakeProvider(), force=True
    ).ok  # insert, then the upsert on re-fetch
    assert refresh_exchange_rates(db, FakeProvider(), force=True).ok
    assert db.scalar(select(ExchangeRate.id).limit(1)) is not None

    assert (
        db.scalar(
            text("SELECT count(*) FROM compensation_records WHERE employee_id = :e"),
            {"e": eid},
        )
        == 5
    )
    assert "permission denied" in denied(
        db, "UPDATE compensation_records SET note = 'x'"
    )


# --- the privilege map is a decision for every table ---


def test_every_table_has_an_explicit_decision():
    assert set(TABLE_PRIVILEGES) == set(Base.metadata.tables), (
        "add the new table to app.db.grants.TABLE_PRIVILEGES"
    )


def test_history_is_read_and_append_only_and_nothing_can_delete():
    assert TABLE_PRIVILEGES["compensation_records"] == ("SELECT", "INSERT")
    for table, privileges in TABLE_PRIVILEGES.items():
        assert not {"DELETE", "TRUNCATE", "ALL"} & set(privileges), table
    assert SEQUENCES == ("employee_code_seq",)


def test_grants_start_by_taking_everything_away():
    statements = grant_statements("acme_app")
    assert statements[0].startswith("REVOKE ALL ON ALL TABLES")
    assert "GRANT SELECT, INSERT ON compensation_records TO acme_app" in statements


@pytest.mark.parametrize(
    "name", ["", "Acme", "acme app", "acme;drop table x", "1acme", "a" * 64, 'acme"']
)
def test_role_names_are_checked_before_they_reach_sql(name):
    with pytest.raises(ValueError):
        grant_statements(name)
