"""Phase 1.3: every model can be created and read back through the API, and database
rules come back as clean 4xx errors rather than 500s."""

import uuid

import pytest
from sqlalchemy import select

from app.api.db_errors import CHECK_MESSAGE, FOREIGN_KEY_MESSAGE, UNIQUE_MESSAGE
from app.models import ChangeReason, CompensationType
from app.seed.change_reasons import load_change_reasons
from app.seed.compensation_types import load_compensation_types
from app.seed.reference import load_reference_data


def unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def catalog(db, client):
    """The 1.2 reference data, loaded inside the test transaction, plus one company."""
    load_reference_data(db)
    load_compensation_types(db)
    load_change_reasons(db)
    db.flush()
    company = client.post("/companies", json={"name": unique("pytest_co")}).json()
    return {
        "company_id": company["id"],
        "base_pay": db.scalar(
            select(CompensationType.id).where(CompensationType.is_base_pay)
        ),
        "bonus": db.scalar(
            select(CompensationType.id).where(
                CompensationType.category == "bonus",
                CompensationType.subtype == "annual",
            )
        ),
        "reasons": {r.code: r.id for r in db.scalars(select(ChangeReason))},
    }


def employee_body(catalog, **overrides) -> dict:
    body = {
        "company_id": catalog["company_id"],
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": f"{unique('ada')}@example.com",
        "department": "Engineering",
        "job_title": "Engineer",
        "job_level": "L3",
        "current_country": "in",
        "currency": "inr",
        "hire_date": "2024-01-15",
    }
    body.update(overrides)
    return body


def record_body(catalog, **overrides) -> dict:
    body = {
        "compensation_type_id": catalog["base_pay"],
        "change_reason_id": catalog["reasons"]["new_hire"],
        "effective_date": "2024-01-15",
        "amount": "150000.00",
        "changed_by": "hr@acme",
    }
    body.update(overrides)
    return body


@pytest.fixture
def employee(client, catalog) -> dict:
    response = client.post("/employees", json=employee_body(catalog))
    assert response.status_code == 201, response.text
    return response.json()


# --- reference data ---


def test_lists_currencies_and_countries(client, catalog):
    currencies = {c["code"] for c in client.get("/currencies").json()}
    countries = {c["code"]: c for c in client.get("/countries").json()}

    assert {"USD", "EUR", "INR"} <= currencies
    assert countries["IN"]["default_currency"] == "INR"


# --- companies ---


def test_company_create_get_list(client):
    name = unique("Globex")
    created = client.post("/companies", json={"name": f"  {name}  "})

    assert created.status_code == 201
    company = created.json()
    assert company["name"] == name  # whitespace stripped
    assert client.get(f"/companies/{company['id']}").json() == company
    assert company in client.get("/companies").json()


def test_duplicate_company_is_409(client):
    name = unique("Initech")
    client.post("/companies", json={"name": name})

    response = client.post("/companies", json={"name": name})

    assert response.status_code == 409
    assert response.json()["detail"] == UNIQUE_MESSAGE.format(
        entity="company", fields="name"
    )


def test_blank_company_name_is_422(client):
    assert client.post("/companies", json={"name": "   "}).status_code == 422


def test_missing_company_is_404(client):
    response = client.get("/companies/-1")
    assert response.status_code == 404
    assert response.json()["detail"] == "company not found"


# --- compensation types ---


def test_create_compensation_type_is_never_base_pay(client, catalog):
    category = unique("bonus")
    response = client.post(
        "/compensation-types",
        json={
            "name": "Signing Bonus",
            "category": category,
            "subtype": "signing",
            "period_months": 12,
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["is_base_pay"] is False
    assert client.get(f"/compensation-types/{created['id']}").json() == created
    assert created in client.get("/compensation-types").json()


def test_compensation_type_rejects_is_base_pay_field(client):
    response = client.post(
        "/compensation-types",
        json={
            "name": "x",
            "category": unique("c"),
            "period_months": 1,
            "is_base_pay": True,
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize("period_months", [0, -1])
def test_compensation_type_period_must_be_positive(client, period_months):
    response = client.post(
        "/compensation-types",
        json={"name": "x", "category": unique("c"), "period_months": period_months},
    )
    assert response.status_code == 422


def test_duplicate_compensation_type_is_409(client, catalog):
    response = client.post(
        "/compensation-types",
        json={
            "name": "Another Annual Bonus",
            "category": "bonus",
            "subtype": "annual",
            "period_months": 12,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == UNIQUE_MESSAGE.format(
        entity="compensation type", fields="category and subtype"
    )


def test_base_pay_type_is_listed_first(client, catalog):
    types = client.get("/compensation-types").json()
    assert types[0]["is_base_pay"] is True


# --- change reasons ---


def test_change_reason_create_normalizes_code(client):
    code = unique("Spot_Award")
    response = client.post(
        "/change-reasons", json={"code": f" {code} ", "label": "Spot award"}
    )

    assert response.status_code == 201
    reason = response.json()
    assert reason["code"] == code.lower()
    assert client.get(f"/change-reasons/{reason['id']}").json() == reason
    assert reason in client.get("/change-reasons").json()


def test_duplicate_change_reason_is_409(client, catalog):
    response = client.post(
        "/change-reasons", json={"code": "correction", "label": "Again"}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == UNIQUE_MESSAGE.format(
        entity="change reason", fields="code"
    )


def test_change_reason_code_must_be_an_identifier(client):
    response = client.post(
        "/change-reasons", json={"code": "not a code!", "label": "x"}
    )
    assert response.status_code == 422


# --- employees ---


def test_employee_create_and_get(client, employee):
    assert employee["code"].startswith("EMP-")
    assert employee["current_country"] == "IN"  # normalized from 'in'
    assert employee["currency"] == "INR"
    assert employee["status"] == "active"
    assert client.get(f"/employees/{employee['id']}").json() == employee


def test_employee_code_cannot_be_client_supplied(client, catalog):
    response = client.post("/employees", json=employee_body(catalog, code="EMP-999999"))

    assert response.status_code == 201
    assert response.json()["code"] != "EMP-999999"


def test_duplicate_email_is_409_case_insensitively(client, catalog):
    email = f"{unique('grace')}@example.com"
    client.post("/employees", json=employee_body(catalog, email=email))

    response = client.post(
        "/employees", json=employee_body(catalog, email=email.upper())
    )

    assert response.status_code == 409
    assert response.json()["detail"] == UNIQUE_MESSAGE.format(
        entity="employee", fields="email"
    )


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"company_id": -1}, FOREIGN_KEY_MESSAGE.format(entity="company")),
        ({"current_country": "QQ"}, FOREIGN_KEY_MESSAGE.format(entity="country")),
        ({"currency": "XYZ"}, FOREIGN_KEY_MESSAGE.format(entity="currency")),
        (
            {"status": "terminated"},
            CHECK_MESSAGE.format(
                entity="employee", rule="termination date matches status"
            ),
        ),
        (
            {"status": "terminated", "termination_date": "2023-01-01"},
            CHECK_MESSAGE.format(entity="employee", rule="termination after hire"),
        ),
    ],
)
def test_employee_db_rules_are_422(client, catalog, overrides, detail):
    response = client.post("/employees", json=employee_body(catalog, **overrides))

    assert response.status_code == 422
    assert response.json()["detail"] == detail


def test_employee_rejects_malformed_email_and_unknown_status(client, catalog):
    assert (
        client.post("/employees", json=employee_body(catalog, email="nope")).status_code
        == 422
    )
    assert (
        client.post(
            "/employees", json=employee_body(catalog, status="retired")
        ).status_code
        == 422
    )


def test_missing_employee_is_404(client):
    assert client.get("/employees/-1").status_code == 404


# --- compensation records ---


def test_record_is_written_in_employees_country_and_currency(client, catalog, employee):
    response = client.post(
        f"/employees/{employee['id']}/compensation-records", json=record_body(catalog)
    )

    assert response.status_code == 201, response.text
    record = response.json()
    assert (record["country"], record["currency"]) == ("IN", "INR")
    assert record["amount"] == "150000.00"
    assert client.get(f"/compensation-records/{record['id']}").json() == record


def test_any_reason_works_with_any_type(client, catalog, employee):
    response = client.post(
        f"/employees/{employee['id']}/compensation-records",
        json=record_body(
            catalog,
            compensation_type_id=catalog["bonus"],
            change_reason_id=catalog["reasons"]["promotion"],
            amount="20000",
        ),
    )
    assert response.status_code == 201


def test_history_is_newest_first(client, catalog, employee):
    url = f"/employees/{employee['id']}/compensation-records"
    client.post(url, json=record_body(catalog))
    client.post(
        url,
        json=record_body(
            catalog,
            change_reason_id=catalog["reasons"]["annual_revision"],
            effective_date="2025-04-01",
            amount="165000",
        ),
    )

    history = client.get(url).json()

    assert [r["effective_date"] for r in history] == ["2025-04-01", "2024-01-15"]


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"amount": "0"}, "base-pay compensation must be greater than zero"),
        ({"effective_date": "2024-01-14"}, "effective_date cannot precede hire_date"),
        ({"change_reason_id": -1}, FOREIGN_KEY_MESSAGE.format(entity="change reason")),
        (
            {"compensation_type_id": -1},
            FOREIGN_KEY_MESSAGE.format(entity="compensation type"),
        ),
    ],
)
def test_record_db_rules_are_422(client, catalog, employee, overrides, detail):
    response = client.post(
        f"/employees/{employee['id']}/compensation-records",
        json=record_body(catalog, **overrides),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == detail


def test_negative_amount_is_rejected_before_the_db(client, catalog, employee):
    response = client.post(
        f"/employees/{employee['id']}/compensation-records",
        json=record_body(catalog, amount="-1"),
    )
    assert response.status_code == 422


def test_record_for_missing_employee_is_404(client, catalog):
    response = client.post(
        "/employees/-1/compensation-records", json=record_body(catalog)
    )
    assert response.status_code == 404


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_records_have_no_update_or_delete_endpoint(client, catalog, employee, method):
    record = client.post(
        f"/employees/{employee['id']}/compensation-records", json=record_body(catalog)
    ).json()

    response = getattr(client, method)(f"/compensation-records/{record['id']}")

    assert response.status_code == 405
