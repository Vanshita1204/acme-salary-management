"""Phase 9 (FR-5): CSV import — template, validate-and-preview, all-or-nothing confirm."""

import csv
import io
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.domain import csv_import as ci
from app.domain.csv_import import EMPLOYEE_COLUMNS, MAX_ROWS
from app.models import (
    ChangeReason,
    Company,
    CompensationRecord,
    CompensationType,
    CurrentCompensation,
    Employee,
)
from app.seed.change_reasons import load_change_reasons
from app.seed.compensation_types import load_compensation_types
from app.seed.reference import load_reference_data
from app.services import imports as import_service
from app.services.compensation import promote_due_records, utc_today
from app.services.errors import Problem, ServiceError


@pytest.fixture
def company(db, client, org) -> str:
    load_reference_data(db)
    load_compensation_types(db)
    load_change_reasons(db)
    name = f"Pytest Imports {uuid.uuid4().hex[:8]}"
    assert client.post("/companies", json={"name": name}).status_code == 201
    return name


BASE_PAY = "Base Pay"  # the catalog's base-pay type: its column is required
COLUMNS = (*EMPLOYEE_COLUMNS, BASE_PAY)


@pytest.fixture
def domain() -> str:
    """A unique email domain per test, so rows never collide with existing data."""
    return f"{uuid.uuid4().hex[:8]}.pytest.example"


def row(company_name: str, domain: str, n: int, **overrides) -> dict[str, str]:
    return {
        "first_name": "Ada",
        "last_name": f"Lovelace{n}",
        "email": f"ada{n}@{domain}",
        "company": company_name,
        "department": "Engineering",
        "job_title": "Software Engineer",
        "job_level": "L3",
        "country": "IN",
        "hire_date": "2024-01-15",
        "currency": "INR",
        BASE_PAY: "150000",
    } | overrides


def to_csv(rows: list[dict], columns=COLUMNS) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def upload(content: bytes) -> dict:
    return {"file": ("employees.csv", content, "text/csv")}


def validate(client, content: bytes) -> dict:
    response = client.post("/import/validate", files=upload(content))
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, content: bytes, changed_by: str = "hr@acme"):
    return client.post(
        "/import/confirm", files=upload(content), data={"changed_by": changed_by}
    )


def count_employees(db, domain: str) -> int:
    return db.scalar(select(func.count()).where(Employee.email.ilike(f"%@{domain}")))


# --- template ---


def test_template_is_the_employee_fields_then_a_column_per_pay_type(
    db, client, company
):
    response = client.get("/import/template")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    header = next(csv.reader(io.StringIO(response.text)))
    pay_columns = header[len(EMPLOYEE_COLUMNS) :]
    assert header[: len(EMPLOYEE_COLUMNS)] == list(EMPLOYEE_COLUMNS)
    assert pay_columns[0] == BASE_PAY  # base pay leads
    assert sorted(pay_columns) == sorted(db.scalars(select(CompensationType.name)))


def test_a_new_compensation_type_appears_in_the_template(db, client, company):
    name = f"Pytest Perk {uuid.uuid4().hex[:6]}"
    created = client.post(
        "/compensation-types",
        json={"category": "perk", "subtype": uuid.uuid4().hex[:6], "name": name,
              "period_months": 1},
    )  # fmt: skip
    assert created.status_code == 201, created.text

    header = next(csv.reader(io.StringIO(client.get("/import/template").text)))

    assert name in header


# --- validate: report, preview, nothing saved ---


def test_validate_previews_a_valid_file_and_saves_nothing(
    db, client, company, domain, org
):
    report = validate(
        client, to_csv([row(company, domain, 1), row(company, domain, 2)])
    )

    assert report["ok"] is True
    assert report["errors"] == []
    assert report["row_count"] == 2
    first = report["preview"][0]
    assert (first["row"], first["email"]) == (2, f"ada1@{domain}")
    assert first["department_id"] == org["departments"]["Engineering"]
    assert first["job_level_id"] == org["levels"]["L3"]
    assert count_employees(db, domain) == 0


def test_validate_reports_every_bad_row_with_row_column_reason(
    db, client, company, domain
):
    rows = [row(company, domain, n) for n in range(1, 6)]
    rows[1]["job_title"] = "SW Engineer"
    rows[3][BASE_PAY] = "0"
    rows[4]["email"] = rows[0]["email"].upper()  # duplicate within the file

    report = validate(client, to_csv(rows))

    assert report["ok"] is False
    assert report["errors"] == [
        {"row": 3, "column": "job_title", "reason": ci.UNKNOWN_JOB_TITLE},
        {"row": 5, "column": BASE_PAY, "reason": ci.AMOUNT_NOT_POSITIVE},
        {
            "row": 6,
            "column": "email",
            "reason": ci.EMAIL_DUPLICATED.format(row=2),
        },
    ]
    assert count_employees(db, domain) == 0


def test_validate_flags_emails_that_already_exist(client, company, domain):
    content = to_csv([row(company, domain, 1)])
    assert confirm(client, content).status_code == 201

    report = validate(client, content)

    assert report["errors"] == [
        {"row": 2, "column": "email", "reason": ci.EMAIL_EXISTS}
    ]


def test_names_match_ignoring_case_and_whitespace(client, company, domain):
    report = validate(
        client,
        to_csv(
            [
                row(
                    company,
                    domain,
                    1,
                    company=f"  {company.upper()} ",
                    department="engineering",
                    job_title=" SOFTWARE ENGINEER",
                    job_level="l3",
                    country="in",
                    currency="inr",
                )
            ]
        ),
    )
    assert report["ok"] is True, report["errors"]


def test_header_is_matched_ignoring_case_and_padding(client, company, domain):
    content = to_csv([row(company, domain, 1)]).replace(
        b"first_name,last_name", b" First_Name , LAST_NAME", 1
    )
    assert validate(client, content)["ok"] is True


def test_missing_columns_are_reported_on_the_header_row(client, company, domain):
    columns = [c for c in COLUMNS if c != "currency"]
    report = validate(client, to_csv([row(company, domain, 1)], columns))

    assert report["errors"] == [
        {"row": 1, "column": "currency", "reason": ci.MISSING_COLUMN}
    ]


def test_an_unknown_column_is_rejected_with_a_hint(client, company, domain):
    columns = (*COLUMNS, "Signing Bonus")
    rows = [row(company, domain, 1, **{"Signing Bonus": "5000"})]

    report = validate(client, to_csv(rows, columns))

    assert report["errors"] == [
        {"row": 1, "column": "signing bonus", "reason": ci.UNKNOWN_COLUMN}
    ]


def test_the_base_pay_column_is_required(client, company, domain):
    columns = EMPLOYEE_COLUMNS
    report = validate(client, to_csv([row(company, domain, 1)], columns))

    assert report["errors"] == [
        {"row": 1, "column": BASE_PAY, "reason": ci.MISSING_COLUMN}
    ]


def test_excel_byte_order_mark_and_blank_lines_are_fine(client, company, domain):
    content = b"\xef\xbb\xbf" + to_csv([row(company, domain, 1)]) + b"\r\n,,,\r\n\r\n"
    report = validate(client, content)

    assert report["ok"] is True
    assert report["row_count"] == 1


def test_preview_is_capped_but_row_count_is_not(client, company, domain):
    rows = [row(company, domain, n) for n in range(150)]
    report = validate(client, to_csv(rows))

    assert report["row_count"] == 150
    assert len(report["preview"]) == 100


# --- the row cap and malformed files ---


def test_more_than_max_rows_is_rejected_before_rows_are_validated(
    client, company, domain
):
    rows = [row(company, domain, n, email="not-an-email") for n in range(MAX_ROWS + 1)]

    report = validate(client, to_csv(rows))

    assert report["errors"] == [
        {
            "row": 1,
            "column": "",
            "reason": ci.TOO_MANY_ROWS.format(count=MAX_ROWS + 1, limit=MAX_ROWS),
        }
    ]


def test_non_utf8_file_is_422(client):
    response = client.post(
        "/import/validate", files=upload("first_name\nJosé\n".encode("latin-1"))
    )
    assert response.status_code == 422
    assert response.json()["detail"] == import_service.NOT_UTF8


def test_oversized_file_is_413(client, monkeypatch):
    monkeypatch.setattr(import_service, "MAX_UPLOAD_BYTES", 10)
    response = client.post("/import/validate", files=upload(b"x" * 11))
    assert response.status_code == 413


def test_upload_is_required(client):
    assert client.post("/import/validate").status_code == 422


# --- confirm: all or nothing ---


def test_confirm_creates_employees_with_new_hire_base_pay(
    db, client, company, domain, org
):
    response = confirm(
        client, to_csv([row(company, domain, 1), row(company, domain, 2)])
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] == 2
    employee = db.get(Employee, body["employee_ids"][0])
    assert (employee.email, employee.status) == (f"ada1@{domain}", "active")
    assert employee.job_title == "Software Engineer"
    record = db.scalars(
        select(CompensationRecord).where(CompensationRecord.employee_id == employee.id)
    ).one()
    reason = db.get(ChangeReason, record.change_reason_id)
    assert (reason.code, record.amount, record.currency, record.changed_by) == (
        "new_hire",
        Decimal("150000.00"),
        "INR",
        "hr@acme",
    )
    assert str(record.effective_date) == "2024-01-15"
    profile = client.get(f"/employees/{employee.id}").json()
    assert Decimal(profile["total_compensation"]) == Decimal("1800000.00")


def test_confirm_saves_a_record_for_every_pay_component_given(
    db, client, company, domain
):
    columns = (*COLUMNS, "Housing Allowance", "Annual Bonus", "Quarterly Bonus")
    rows = [
        row(company, domain, 1, **{"Housing Allowance": "20000", "Annual Bonus": "50000"}),
        row(company, domain, 2),  # base pay only: the other cells are blank
    ]  # fmt: skip

    response = confirm(client, to_csv(rows, columns))

    assert response.status_code == 201, response.text
    first, second = response.json()["employee_ids"]

    def pay(employee_id):
        return {
            name: amount
            for name, amount in db.execute(
                select(CompensationType.name, CurrentCompensation.amount)
                .join(CompensationType)
                .where(CurrentCompensation.employee_id == employee_id)
            )
        }

    assert pay(first) == {
        BASE_PAY: Decimal("150000.00"),
        "Housing Allowance": Decimal("20000.00"),
        "Annual Bonus": Decimal("50000.00"),
    }
    assert pay(second) == {BASE_PAY: Decimal("150000.00")}
    # 150,000 x 12 + 20,000 x 12 (monthly) + 50,000 (annual) = 2,090,000
    profile = client.get(f"/employees/{first}").json()
    assert Decimal(profile["total_compensation"]) == Decimal("2090000.00")


def test_validate_previews_every_pay_component(client, company, domain):
    columns = (*COLUMNS, "Housing Allowance")
    rows = [row(company, domain, 1, **{"Housing Allowance": "20000"})]

    report = validate(client, to_csv(rows, columns))

    assert [(p["name"], Decimal(p["amount"])) for p in report["preview"][0]["pay"]] == [
        (BASE_PAY, Decimal(150000)),
        ("Housing Allowance", Decimal(20000)),
    ]


def test_one_bad_row_saves_nothing(db, client, company, domain):
    rows = [row(company, domain, n) for n in range(1, 51)]
    rows[36]["country"] = "QQ"

    response = confirm(client, to_csv(rows))

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"row": 38, "column": "country", "reason": ci.UNKNOWN_COUNTRY}
    ]
    assert count_employees(db, domain) == 0


def test_confirm_revalidates_instead_of_trusting_the_preview(
    db, client, company, domain, org
):
    content = to_csv([row(company, domain, 1), row(company, domain, 2)])
    assert validate(client, content)["ok"] is True
    # Someone adds one of these people by hand between preview and confirm.
    taken = row(company, domain, 2)
    created = client.post(
        "/employees",
        json={
            "company_id": db.scalar(select(Company.id).where(Company.name == company)),
            "first_name": "Ada",
            "last_name": "Lovelace2",
            "email": taken["email"],
            **org["role"],
            "current_country": "IN",
            "currency": "INR",
            "hire_date": "2024-01-15",
            "base_pay": {"amount": "150000"},
            "changed_by": "hr@acme",
        },
    )
    assert created.status_code == 201, created.text

    response = confirm(client, content)

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"row": 3, "column": "email", "reason": ci.EMAIL_EXISTS}
    ]
    assert count_employees(db, domain) == 1  # only the hand-made one


def test_failure_while_inserting_rolls_everything_back(
    db, client, company, domain, monkeypatch
):
    def failing_reason_id(session, code):
        raise ServiceError(Problem.INVALID, "simulated failure")

    monkeypatch.setattr(import_service, "reason_id", failing_reason_id)

    response = confirm(client, to_csv([row(company, domain, 1)]))

    assert response.status_code == 422
    assert count_employees(db, domain) == 0  # employee rows were inserted, then undone


def test_confirm_requires_changed_by(client, company, domain):
    content = to_csv([row(company, domain, 1)])
    assert confirm(client, content, changed_by="  ").status_code == 422
    response = client.post("/import/confirm", files=upload(content))
    assert response.status_code == 422


def test_future_hire_date_becomes_current_on_that_date(db, client, company, domain):
    start = utc_today() + timedelta(days=14)
    response = confirm(client, to_csv([row(company, domain, 1, hire_date=str(start))]))
    employee_id = response.json()["employee_ids"][0]

    def current():
        return db.scalar(
            select(func.count()).where(CurrentCompensation.employee_id == employee_id)
        )

    assert current() == 0
    promote_due_records(db, start)
    assert current() == 1
