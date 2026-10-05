"""Every way a request can fail answers in one shape the frontend understands:
`{"detail": "a sentence"}` or `{"detail": [{"loc": [...], "msg": "..."}]}`. Never HTML or
plain text, and never the text of an internal exception."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.api.employees as employees_api
from app.api.deps import (
    DbSession,  # noqa: F401  (documents which dependency is swapped below)
)
from app.db.session import get_db
from app.main import DATABASE_UNAVAILABLE, UNEXPECTED_ERROR, app

SECRET = "password=hunter2 host=10.0.0.5"


@pytest.fixture
def forgiving(client):
    """A client that returns 500 responses instead of raising them (like a real server)."""
    return TestClient(app, raise_server_exceptions=False)


def assert_contract(response):
    assert response.headers["content-type"].startswith("application/json"), (
        response.text
    )
    detail = response.json()["detail"]
    if isinstance(detail, list):
        assert detail and all(
            isinstance(item["loc"], list) and isinstance(item["msg"], str)
            for item in detail
        )
    else:
        assert isinstance(detail, str) and detail


CASES = [
    ("unknown route", "get", "/nope", {}, 404),
    ("wrong method", "put", "/employees/1", {}, 405),
    (
        "malformed JSON",
        "post",
        "/employees",
        {"content": "{oops", "headers": {"content-type": "application/json"}},
        422,
    ),
    ("missing fields", "post", "/employees", {"json": {}}, 422),
    ("path parameter of the wrong type", "get", "/employees/abc", {}, 422),
    (
        "query parameter out of range",
        "get",
        "/employees",
        {"params": {"limit": 0}},
        422,
    ),
    ("unknown employee", "get", "/employees/-1", {}, 404),
    (
        "unknown employee, action",
        "post",
        "/employees/-1/terminate",
        {"json": {"termination_date": "2024-01-01"}},
        404,
    ),
    ("tampered cursor", "get", "/employees", {"params": {"cursor": "garbage"}}, 400),
    (
        "unsupported reporting currency",
        "get",
        "/employees",
        {"params": {"reporting_currency": "ZZZ"}},
        422,
    ),
    (
        "unsupported reporting currency, analytics",
        "get",
        "/analytics/summary",
        {"params": {"reporting_currency": "ZZZ"}},
        422,
    ),
    (
        "conversion of an unknown currency",
        "get",
        "/exchange-rates/convert",
        {"params": {"amount": 1, "from": "ZZZ", "to": "USD"}},
        422,
    ),
    ("import without a file", "post", "/import/validate", {}, 422),
    (
        "import that isn't UTF-8",
        "post",
        "/import/validate",
        {"files": {"file": ("a.csv", "é".encode("latin-1"), "text/csv")}},
        422,
    ),
    (
        "backwards date range",
        "get",
        "/analytics/changes",
        {"params": {"date_from": "2025-02-01", "date_to": "2025-01-01"}},
        422,
    ),
    ("unknown reference entry", "get", "/job-levels/-1", {}, 404),
    ("empty name", "post", "/departments", {"json": {"name": "  "}}, 422),
    (
        "field that must not be sent",
        "patch",
        "/employees/1",
        {"json": {"currency": "USD"}},
        422,
    ),
]


@pytest.mark.parametrize(
    ("label", "method", "path", "kwargs", "status"), CASES, ids=[c[0] for c in CASES]
)
def test_every_handled_failure_has_the_shared_shape(
    client, label, method, path, kwargs, status
):
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == status, response.text
    assert_contract(response)


def test_database_rule_violations_are_sentences_not_driver_errors(client, db, org):
    from app.seed.reference import load_reference_data

    load_reference_data(db)
    body = {
        "company_id": -1,
        "first_name": "A",
        "last_name": "B",
        "email": "a@b.example",
        **org["role"],
        "current_country": "IN",
        "currency": "INR",
        "hire_date": "2024-01-01",
        "base_pay": {"amount": "1"},
        "changed_by": "x",
    }
    response = client.post("/employees", json=body)
    assert response.status_code == 422
    assert_contract(response)
    assert "psycopg" not in response.text and "SQL" not in response.text


def test_an_unexpected_failure_is_json_and_leaks_nothing(forgiving, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError(f"internal detail: {SECRET}")

    monkeypatch.setattr(employees_api, "list_employees", broken)

    response = forgiving.get("/employees")

    assert response.status_code == 500
    assert_contract(response)
    assert response.json() == {"detail": UNEXPECTED_ERROR}
    assert SECRET not in response.text and "RuntimeError" not in response.text


def test_a_lost_database_connection_is_a_503_with_a_retry_message(forgiving):
    def no_database():
        raise OperationalError(
            "SELECT 1", {}, Exception(f"could not connect: {SECRET}")
        )
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = no_database
    try:
        response = forgiving.get("/employees")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert_contract(response)
    assert response.json() == {"detail": DATABASE_UNAVAILABLE}
    assert SECRET not in response.text


def test_a_failure_part_way_through_a_write_saves_nothing(client, db, org, monkeypatch):
    """The unexpected-error path must not leave half a change behind."""
    from sqlalchemy import func, select

    from app.models import Employee
    from app.seed.change_reasons import load_change_reasons
    from app.seed.compensation_types import load_compensation_types
    from app.seed.reference import load_reference_data
    from app.services import employees as employee_service

    load_reference_data(db)
    load_compensation_types(db)
    load_change_reasons(db)
    company = client.post("/companies", json={"name": "Atomic test"}).json()

    def fail(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(employee_service, "append_record", fail)
    forgiving = TestClient(app, raise_server_exceptions=False)
    body = {
        "company_id": company["id"],
        "first_name": "A",
        "last_name": "B",
        "email": "atomic@b.example",
        **org["role"],
        "current_country": "IN",
        "currency": "INR",
        "hire_date": "2024-01-01",
        "base_pay": {"amount": "1"},
        "changed_by": "x",
    }
    response = forgiving.post("/employees", json=body)

    assert response.status_code == 500
    assert (
        db.scalar(select(func.count()).where(Employee.email == "atomic@b.example")) == 0
    )
