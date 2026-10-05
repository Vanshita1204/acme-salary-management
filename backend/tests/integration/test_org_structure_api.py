"""Phase 7A: departments, job titles and levels as reference data, and their use on
employees."""

import uuid

import pytest

from app.api.db_errors import UNIQUE_MESSAGE
from app.seed.org_structure import LEVEL_CODES
from app.services.employees import (
    UNKNOWN_DEPARTMENT,
    UNKNOWN_JOB_LEVEL,
    UNKNOWN_JOB_TITLE,
)


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


@pytest.fixture
def company(client) -> int:
    return client.post("/companies", json={"name": unique("pytest co")}).json()["id"]


def employee_body(org, company, **overrides) -> dict:
    return {
        "company_id": company,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": f"{uuid.uuid4().hex[:10]}@pytest.example",
        **org["role"],
        "current_country": "IN",
        "currency": "INR",
        "hire_date": "2024-01-15",
        "base_pay": {"amount": "150000"},
        "changed_by": "hr@acme",
    } | overrides


# --- reference endpoints ---


@pytest.mark.parametrize("path", ["/departments", "/job-titles"])
def test_create_get_and_list_named_entries(client, path):
    name = unique("Pytest")

    created = client.post(path, json={"name": f"  {name}  "})

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == name  # trimmed
    assert client.get(f"{path}/{body['id']}").json() == body
    assert body in client.get(path).json()


@pytest.mark.parametrize(
    ("path", "entity"), [("/departments", "department"), ("/job-titles", "job title")]
)
def test_case_variant_duplicate_is_409(client, path, entity):
    name = unique("Pytest")
    assert client.post(path, json={"name": name}).status_code == 201

    response = client.post(path, json={"name": name.upper()})

    assert response.status_code == 409
    assert response.json()["detail"] == UNIQUE_MESSAGE.format(
        entity=entity, fields="name"
    )


@pytest.mark.parametrize("path", ["/departments", "/job-titles"])
def test_rename_keeps_employees_pointing_at_it(client, org, company, path):
    field = "department_id" if path == "/departments" else "job_title_id"
    entry = client.post(path, json={"name": unique("Pytest")}).json()
    employee = client.post(
        "/employees", json=employee_body(org, company, **{field: entry["id"]})
    ).json()
    new_name = unique("Renamed")

    renamed = client.patch(f"{path}/{entry['id']}", json={"name": new_name})

    assert renamed.status_code == 200
    profile = client.get(f"/employees/{employee['id']}").json()["employee"]
    assert profile[field.removesuffix("_id")] == new_name


@pytest.mark.parametrize("path", ["/departments", "/job-titles", "/job-levels"])
def test_entries_cannot_be_deleted(client, path):
    entry_id = client.get(path).json()[0]["id"]
    assert client.delete(f"{path}/{entry_id}").status_code == 405


@pytest.mark.parametrize("path", ["/departments", "/job-titles", "/job-levels"])
def test_missing_entry_is_404(client, path):
    assert client.get(f"{path}/-1").status_code == 404


def test_levels_are_listed_by_rank(client, org):
    levels = client.get("/job-levels").json()

    assert [lvl["rank"] for lvl in levels] == sorted(lvl["rank"] for lvl in levels)
    seeded = [lvl["code"] for lvl in levels if lvl["code"] in LEVEL_CODES]
    assert seeded == LEVEL_CODES


def test_level_with_taken_rank_or_code_is_409(client, org):
    rank = 3_000_000 + uuid.uuid4().int % 10**6
    code = unique("P")
    assert (
        client.post(
            "/job-levels", json={"code": code, "label": "x", "rank": rank}
        ).status_code
        == 201
    )

    assert (
        client.post(
            "/job-levels", json={"code": unique("P"), "label": "x", "rank": rank}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/job-levels", json={"code": code.lower(), "label": "x", "rank": rank + 1}
        ).status_code
        == 409
    )


def test_level_can_be_relabelled_and_reranked(client):
    rank = 4_000_000 + uuid.uuid4().int % 10**6
    level = client.post(
        "/job-levels", json={"code": unique("P"), "label": "Old", "rank": rank}
    ).json()

    response = client.patch(
        f"/job-levels/{level['id']}", json={"label": "New", "rank": rank + 1}
    )

    assert response.status_code == 200
    assert (response.json()["label"], response.json()["rank"]) == ("New", rank + 1)


@pytest.mark.parametrize(
    "body",
    [{"name": "  "}, {"name": "X", "extra": 1}, {}],
)
def test_invalid_names_are_422(client, body):
    assert client.post("/departments", json=body).status_code == 422


# --- employees reference them ---


def test_employee_carries_ids_and_names(client, org, company):
    response = client.post("/employees", json=employee_body(org, company))

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["department"], body["job_title"], body["job_level"]) == (
        "Engineering",
        "Software Engineer",
        "L3",
    )
    assert body["department_id"] == org["role"]["department_id"]


@pytest.mark.parametrize(
    ("field", "detail"),
    [
        ("department_id", UNKNOWN_DEPARTMENT),
        ("job_title_id", UNKNOWN_JOB_TITLE),
        ("job_level_id", UNKNOWN_JOB_LEVEL),
    ],
)
def test_unknown_reference_is_422_on_create_and_edit(
    client, org, company, field, detail
):
    created = client.post("/employees", json=employee_body(org, company, **{field: -1}))
    assert created.status_code == 422
    assert created.json()["detail"] == detail

    employee = client.post("/employees", json=employee_body(org, company)).json()
    edited = client.patch(f"/employees/{employee['id']}", json={field: -1})
    assert edited.status_code == 422
    assert edited.json()["detail"] == detail


@pytest.mark.parametrize("field", ["department", "job_title", "job_level"])
def test_free_text_role_fields_are_rejected(client, org, company, field):
    body = employee_body(org, company, **{field: "Engineering"})
    assert client.post("/employees", json=body).status_code == 422


def test_edit_moves_employee_to_another_department(client, org, company):
    employee = client.post("/employees", json=employee_body(org, company)).json()

    response = client.patch(
        f"/employees/{employee['id']}",
        json={"department_id": org["departments"]["Product"]},
    )

    assert response.status_code == 200
    assert response.json()["department"] == "Product"
