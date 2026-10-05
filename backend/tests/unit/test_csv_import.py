from datetime import date
from decimal import Decimal

import pytest

from app.domain import csv_import as ci
from app.domain.csv_import import ImportContext, RowError, validate_import

CONTEXT = ImportContext.build(
    companies={"Globex": 1, "Initech": 2},
    countries=["IN", "US"],
    currencies=["INR", "USD"],
    existing_emails=["taken@example.com"],
)


def record(**overrides) -> dict[str, str]:
    base = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "company": "Globex",
        "department": "Engineering",
        "job_title": "Engineer",
        "job_level": "L3",
        "country": "IN",
        "hire_date": "2024-01-15",
        "currency": "INR",
        "base_pay_amount": "150000",
    }
    base.update(overrides)
    return base


def run(*records, header=ci.TEMPLATE_COLUMNS):
    return validate_import(header, list(records), CONTEXT)


def test_valid_file_parses_every_row():
    result = run(
        record(),
        record(
            email="grace@example.com", company="initech", country="us", currency="usd"
        ),
    )

    assert result.ok
    first, second = result.rows
    assert first.row == 2  # header is row 1
    assert first.company_id == 1
    assert first.hire_date == date(2024, 1, 15)
    assert first.base_pay_amount == Decimal(150000)
    assert (second.company_id, second.country, second.currency) == (2, "US", "USD")


def test_missing_header_columns_are_reported_on_row_1():
    header = [c for c in ci.TEMPLATE_COLUMNS if c != "currency"]
    result = run(record(), header=header)

    assert result.errors == [RowError(1, "currency", ci.MISSING_COLUMN)]
    assert result.rows == []


def test_row_cap():
    result = validate_import(
        ci.TEMPLATE_COLUMNS, [record()] * (ci.MAX_ROWS + 1), CONTEXT
    )
    assert len(result.errors) == 1 and result.errors[0].row == 1


@pytest.mark.parametrize(
    ("overrides", "column", "reason"),
    [
        ({"first_name": "  "}, "first_name", ci.REQUIRED),
        ({"email": "not-an-email"}, "email", ci.INVALID_EMAIL),
        ({"email": "TAKEN@example.com"}, "email", ci.EMAIL_EXISTS),
        ({"company": "Umbrella"}, "company", ci.UNKNOWN_COMPANY),
        ({"country": "QQ"}, "country", ci.UNKNOWN_COUNTRY),
        ({"currency": "XYZ"}, "currency", ci.UNSUPPORTED_CURRENCY),
        ({"hire_date": "15/01/2024"}, "hire_date", ci.INVALID_DATE),
        ({"base_pay_amount": "abc"}, "base_pay_amount", ci.INVALID_AMOUNT),
        ({"base_pay_amount": "NaN"}, "base_pay_amount", ci.INVALID_AMOUNT),
        ({"base_pay_amount": "0"}, "base_pay_amount", ci.AMOUNT_NOT_POSITIVE),
        ({"base_pay_amount": "-5"}, "base_pay_amount", ci.AMOUNT_NOT_POSITIVE),
        ({"base_pay_amount": "10.555"}, "base_pay_amount", ci.TOO_MANY_DECIMALS),
    ],
)
def test_row_rules(overrides, column, reason):
    result = run(record(**overrides))

    assert result.errors == [RowError(2, column, reason)]


def test_thousands_separators_are_accepted():
    assert run(record(base_pay_amount="1,50,000.50")).rows[
        0
    ].base_pay_amount == Decimal("150000.50")


def test_email_duplicated_within_file_points_at_first_occurrence():
    result = run(record(), record(email="ADA@example.com"))

    assert result.errors == [RowError(3, "email", ci.EMAIL_DUPLICATED.format(row=2))]


def test_one_bad_row_makes_the_file_not_ok_and_reports_every_problem():
    result = run(
        record(), record(email="x", currency="XYZ"), record(email="z@example.com")
    )

    assert not result.ok
    assert {(e.row, e.column) for e in result.errors} == {(3, "email"), (3, "currency")}
