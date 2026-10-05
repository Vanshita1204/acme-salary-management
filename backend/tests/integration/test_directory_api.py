"""GET /employees (FR-1): search, filters, sorts, keyset pagination, reporting totals."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    ChangeReason,
    Company,
    CompensationRecord,
    CompensationType,
    CurrentCompensation,
    Employee,
    ExchangeRate,
)
from app.seed.change_reasons import load_change_reasons
from app.seed.companies import load_companies
from app.seed.compensation_types import load_compensation_types
from app.seed.reference import load_reference_data

FAR_FUTURE = date(
    2099, 1, 1
)  # fake rates dated after any real ones, so they're "latest"
RATES = {"USD": Decimal(1), "EUR": Decimal("1.25"), "INR": Decimal("0.0125")}


@pytest.fixture
def world(db):
    """A private department of employees with known pay, plus fixed exchange rates."""
    load_reference_data(db)
    load_companies(db, 1)
    load_compensation_types(db)
    load_change_reasons(db)
    db.add_all(
        ExchangeRate(currency=c, rate_to_usd=r, rate_date=FAR_FUTURE, source="pytest")
        for c, r in RATES.items()
    )
    db.flush()
    types = {(t.category, t.subtype): t for t in db.scalars(select(CompensationType))}
    reason = db.scalar(select(ChangeReason).where(ChangeReason.code == "new_hire"))
    company_id = db.scalar(select(Company.id).limit(1))
    department = f"pytest-{uuid.uuid4().hex[:8]}"

    def hire(
        first,
        last,
        country,
        currency,
        hired,
        monthly_base,
        *,
        wellness=None,
        status="active",
        title="Engineer",
    ):
        employee = Employee(
            company_id=company_id,
            first_name=first,
            last_name=last,
            email=f"{uuid.uuid4().hex[:10]}@pytest.example",
            department=department,
            job_title=title,
            job_level="L3",
            current_country=country,
            currency=currency,
            status=status,
            hire_date=hired,
            termination_date=date(2025, 1, 1) if status == "terminated" else None,
        )
        db.add(employee)
        db.flush()
        components = [(types[("fixed", "base")], monthly_base)]
        if wellness:  # an annual reimbursement outside CTC
            components.append((types[("reimbursement", "wellness")], wellness))
        for comp_type, amount in components:
            record = CompensationRecord(
                employee_id=employee.id,
                compensation_type_id=comp_type.id,
                effective_date=hired,
                country=country,
                currency=currency,
                amount=Decimal(amount),
                change_reason_id=reason.id,
                changed_by="pytest",
            )
            db.add(record)
            db.flush()
            db.add(
                CurrentCompensation(
                    employee_id=employee.id,
                    compensation_type_id=comp_type.id,
                    compensation_record_id=record.id,
                    effective_date=hired,
                    amount=record.amount,
                )
            )
        db.flush()
        return employee

    people = {
        "ada": hire(
            "Ada", "Lovelace", "GB", "EUR", date(2020, 3, 1), 8000, wellness=5000
        ),  # 96k EUR = 120k USD
        "alan": hire("Alan", "Turing", "GB", "USD", date(2018, 6, 1), 9000),  # 108k USD
        "grace": hire(
            "Grace", "Hopper", "US", "USD", date(2022, 1, 10), 12500, title="Manager"
        ),  # 150k USD
        "ravi": hire(
            "Ravi", "Kumar", "IN", "INR", date(2021, 9, 1), 600000
        ),  # 7.2M INR = 90k USD
        "edsger": hire(
            "Edsger",
            "Dijkstra",
            "NL",
            "EUR",
            date(2015, 2, 1),
            7000,
            status="terminated",
        ),  # 84k EUR = 105k USD
    }
    return {"department": department, "people": people}


def directory(client, world, **params):
    response = client.get(
        "/employees", params={"department": world["department"], **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def last_names(page):
    return [item["last_name"] for item in page["items"]]


def test_default_sort_is_name(client, world):
    assert last_names(directory(client, world)) == [
        "Dijkstra",
        "Hopper",
        "Kumar",
        "Lovelace",
        "Turing",
    ]


def test_sort_by_hire_date_descending(client, world):
    page = directory(client, world, sort="hire_date", order="desc")
    assert last_names(page) == ["Hopper", "Kumar", "Lovelace", "Turing", "Dijkstra"]


def test_sort_by_compensation_compares_across_currencies(client, world):
    # USD equivalents: Hopper 150k, Lovelace 120k, Turing 108k, Dijkstra 105k, Kumar 90k
    page = directory(client, world, sort="compensation", order="desc")
    assert last_names(page) == ["Hopper", "Lovelace", "Turing", "Dijkstra", "Kumar"]


def test_totals_are_annual_ctc_in_local_and_reporting_currency(client, world):
    items = {
        i["last_name"]: i
        for i in directory(client, world, reporting_currency="eur")["items"]
    }

    ada = items[
        "Lovelace"
    ]  # wellness reimbursement is outside CTC, so not in the total
    assert (Decimal(ada["total_compensation"]), ada["currency"]) == (
        Decimal("96000.00"),
        "EUR",
    )
    assert Decimal(ada["total_compensation_reporting"]) == Decimal("96000.00")
    ravi = items["Kumar"]  # 7.2M INR = 90k USD = 72k EUR
    assert Decimal(ravi["total_compensation"]) == Decimal("7200000.00")
    assert Decimal(ravi["total_compensation_reporting"]) == Decimal("72000.00")


def test_rates_as_of_covers_currencies_used(client, world):
    page = directory(client, world, reporting_currency="EUR")
    assert page["reporting_currency"] == "EUR"
    assert set(page["rates_as_of"]) == {"EUR", "USD", "INR"}
    assert set(page["rates_as_of"].values()) == {"2099-01-01"}


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"country": "gb"}, ["Lovelace", "Turing"]),
        ({"country": ["GB", "IN"]}, ["Kumar", "Lovelace", "Turing"]),
        ({"status": "terminated"}, ["Dijkstra"]),
        ({"job_title": "Manager"}, ["Hopper"]),
        ({"country": "GB", "q": "ada"}, ["Lovelace"]),
        ({"q": "grace hopper"}, ["Hopper"]),  # full name
        ({"q": "TURING"}, ["Turing"]),  # case-insensitive
        ({"q": "100%"}, []),  # LIKE wildcards are literal
    ],
)
def test_filters_and_search_combine(client, world, params, expected):
    assert last_names(directory(client, world, **params)) == expected


def test_search_by_code_and_email(client, world):
    alan = world["people"]["alan"]
    assert last_names(directory(client, world, q=alan.code)) == ["Turing"]
    assert last_names(directory(client, world, q=alan.email.split("@")[0])) == [
        "Turing"
    ]


@pytest.mark.parametrize(
    ("sort", "order"),
    [("name", "asc"), ("hire_date", "desc"), ("compensation", "desc")],
)
def test_paging_forward_then_back_visits_every_row_once(client, world, sort, order):
    full = last_names(directory(client, world, sort=sort, order=order))

    pages, page = [], directory(client, world, sort=sort, order=order, limit=2)
    assert page["prev_cursor"] is None
    while True:
        pages.append(last_names(page))
        if not page["next_cursor"]:
            break
        page = directory(
            client, world, sort=sort, order=order, limit=2, cursor=page["next_cursor"]
        )
    assert [n for p in pages for n in p] == full
    assert [len(p) for p in pages] == [2, 2, 1]

    back = []
    while page["prev_cursor"]:
        page = directory(
            client, world, sort=sort, order=order, limit=2, cursor=page["prev_cursor"]
        )
        back.append(last_names(page))
    assert back == pages[-2::-1]  # same pages, in reverse
    assert page["prev_cursor"] is None and page["next_cursor"]


def test_cursor_from_another_sort_is_rejected(client, world):
    page = directory(client, world, limit=2)
    response = client.get(
        "/employees", params={"cursor": page["next_cursor"], "sort": "hire_date"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "cursor",
    [
        "garbage",
        "eyJzIjoibmFtZSJ9",
        "eyJzIjoibmFtZSIsIm8iOiJhc2MiLCJkIjoiYWZ0ZXIiLCJrIjpbImEiXX0",
    ],
)
def test_malformed_cursors_are_400(client, cursor):
    assert client.get("/employees", params={"cursor": cursor}).status_code == 400


def test_limit_is_bounded(client):
    assert client.get("/employees", params={"limit": 0}).status_code == 422
    assert client.get("/employees", params={"limit": 201}).status_code == 422
