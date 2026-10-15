from datetime import date
from decimal import Decimal

import pytest

from app.domain import csv_import as ci
from app.domain.csv_import import (
    ImportContext,
    PayAmount,
    PayType,
    RowError,
    validate_import,
)

CONTEXT = ImportContext.build(
    companies={"Globex": 1, "Initech": 2},
    countries=["IN", "US"],
    currencies=["INR", "USD"],
    existing_emails=["taken@example.com"],
    departments={"Engineering": 10, "Sales": 11},
    job_titles={"Software Engineer": 20, "Account Executive": 21},
    job_levels={"L3": 30, "L4": 31},
    pay_types=[
        PayType(100, "Base Pay", True),
        PayType(101, "Housing Allowance", False),
        PayType(102, "Annual Bonus", False),
    ],
)
BASE_PAY = "base pay"
DEFAULT_HEADER = (*ci.EMPLOYEE_COLUMNS, BASE_PAY)


def record(**overrides) -> dict[str, str]:
    base = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "company": "Globex",
        "department": "Engineering",
        "job_title": "Software Engineer",
        "job_level": "L3",
        "country": "IN",
        "hire_date": "2024-01-15",
        "currency": "INR",
        BASE_PAY: "150000",
    }
    base.update(overrides)
    return base


def run(*records, header=DEFAULT_HEADER):
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
    assert (first.department_id, first.job_title_id, first.job_level_id) == (10, 20, 30)
    assert first.hire_date == date(2024, 1, 15)
    assert first.pay == (PayAmount(100, "Base Pay", Decimal(150000)),)
    assert (second.company_id, second.country, second.currency) == (2, "US", "USD")


def test_missing_header_columns_are_reported_on_row_1():
    header = [c for c in DEFAULT_HEADER if c != "currency"]
    result = run(record(), header=header)

    assert result.errors == [RowError(1, "currency", ci.MISSING_COLUMN)]
    assert result.rows == []


def test_row_cap():
    result = validate_import(DEFAULT_HEADER, [record()] * (ci.MAX_ROWS + 1), CONTEXT)
    assert len(result.errors) == 1 and result.errors[0].row == 1


@pytest.mark.parametrize(
    ("overrides", "column", "reason"),
    [
        ({BASE_PAY: ""}, "Base Pay", ci.REQUIRED),
        ({"first_name": "  "}, "first_name", ci.REQUIRED),
        ({"email": "not-an-email"}, "email", ci.INVALID_EMAIL),
        ({"email": "TAKEN@example.com"}, "email", ci.EMAIL_EXISTS),
        ({"company": "Umbrella"}, "company", ci.UNKNOWN_COMPANY),
        ({"department": "Engineerin"}, "department", ci.UNKNOWN_DEPARTMENT),
        ({"job_title": "SW Engineer"}, "job_title", ci.UNKNOWN_JOB_TITLE),
        ({"job_level": "L10"}, "job_level", ci.UNKNOWN_JOB_LEVEL),
        ({"country": "QQ"}, "country", ci.UNKNOWN_COUNTRY),
        ({"currency": "XYZ"}, "currency", ci.UNSUPPORTED_CURRENCY),
        ({"hire_date": "15/01/2024"}, "hire_date", ci.INVALID_DATE),
        ({BASE_PAY: "abc"}, "Base Pay", ci.INVALID_AMOUNT),
        ({BASE_PAY: "NaN"}, "Base Pay", ci.INVALID_AMOUNT),
        ({BASE_PAY: "0"}, "Base Pay", ci.AMOUNT_NOT_POSITIVE),
        ({BASE_PAY: "-5"}, "Base Pay", ci.AMOUNT_NOT_POSITIVE),
        ({BASE_PAY: "10.555"}, "Base Pay", ci.TOO_MANY_DECIMALS),
    ],
)
def test_row_rules(overrides, column, reason):
    result = run(record(**overrides))

    assert result.errors == [RowError(2, column, reason)]


def test_thousands_separators_are_accepted():
    rows = run(record(**{BASE_PAY: "1,50,000.50"})).rows
    assert rows[0].pay[0].amount == Decimal("150000.50")


def test_email_duplicated_within_file_points_at_first_occurrence():
    result = run(record(), record(email="ADA@example.com"))

    assert result.errors == [RowError(3, "email", ci.EMAIL_DUPLICATED.format(row=2))]


def test_one_bad_row_makes_the_file_not_ok_and_reports_every_problem():
    result = run(
        record(), record(email="x", currency="XYZ"), record(email="z@example.com")
    )

    assert not result.ok
    assert {(e.row, e.column) for e in result.errors} == {(3, "email"), (3, "currency")}


def test_org_names_match_ignoring_case_and_whitespace():
    result = run(
        record(
            department=" engineering ", job_title="SOFTWARE ENGINEER", job_level="l4"
        )
    )

    assert result.ok
    row = result.rows[0]
    assert (row.department_id, row.job_title_id, row.job_level_id) == (10, 20, 31)


PAY_HEADER = (*DEFAULT_HEADER, "housing allowance", "annual bonus")


def test_each_pay_type_has_its_own_column_and_blank_means_none():
    result = run(
        record(**{"housing allowance": "20,000", "annual bonus": ""}),
        record(email="grace@example.com", **{"annual bonus": "50000.50"}),
        header=PAY_HEADER,
    )

    assert result.ok
    first, second = result.rows
    assert first.pay == (
        PayAmount(100, "Base Pay", Decimal(150000)),
        PayAmount(101, "Housing Allowance", Decimal(20000)),
    )
    assert second.pay == (
        PayAmount(100, "Base Pay", Decimal(150000)),
        PayAmount(102, "Annual Bonus", Decimal("50000.50")),
    )


def test_pay_columns_match_type_names_ignoring_case():
    header = (*ci.EMPLOYEE_COLUMNS, "BASE PAY", " Housing Allowance ")
    result = run(record(**{"base pay": "100", "housing allowance": "5"}), header=header)

    assert result.ok and len(result.rows[0].pay) == 2


def test_a_column_that_is_not_a_known_type_is_an_error_on_row_1():
    result = run(record(), header=(*DEFAULT_HEADER, "Signing Bonus"))

    assert result.errors == [RowError(1, "signing bonus", ci.UNKNOWN_COLUMN)]
    assert result.rows == []


def test_the_base_pay_column_is_required_in_the_header():
    header = tuple(c for c in DEFAULT_HEADER if c != BASE_PAY)
    result = run(record(), header=header)

    assert result.errors == [RowError(1, "Base Pay", ci.MISSING_COLUMN)]


def test_no_base_pay_type_in_the_catalog_is_a_file_error():
    context = ImportContext.build(
        companies={}, countries=[], currencies=[], existing_emails=[],
        departments={}, job_titles={}, job_levels={},
        pay_types=[PayType(101, "Housing Allowance", False)],
    )  # fmt: skip
    result = validate_import(ci.EMPLOYEE_COLUMNS, [], context)

    assert RowError(1, "", ci.NO_BASE_PAY_TYPE) in result.errors


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("-1", ci.AMOUNT_NEGATIVE),
        ("abc", ci.INVALID_AMOUNT),
        ("1.234", ci.TOO_MANY_DECIMALS),
    ],
)
def test_other_pay_amounts_are_checked_under_their_own_column(value, reason):
    result = run(record(**{"housing allowance": value}), header=PAY_HEADER)

    assert result.errors == [RowError(2, "Housing Allowance", reason)]


def test_zero_is_allowed_for_other_pay_but_not_base_pay():
    assert run(record(**{"housing allowance": "0"}), header=PAY_HEADER).ok
    assert not run(record(**{BASE_PAY: "0"}), header=PAY_HEADER).ok


def test_unknown_companies_are_listed_once_each_as_written():
    result = run(
        record(email="a@example.com", company="Umbrella"),
        record(email="b@example.com", company="umbrella"),
        record(email="c@example.com", company="Hooli"),
        record(email="d@example.com", company="Globex"),
        record(email="e@example.com", company="Hooli", department="Nowhere"),
    )

    assert result.unknown_companies == ["Umbrella", "Hooli"]
    assert not result.ok  # still rejected until the companies exist


def test_a_file_of_known_companies_lists_none():
    assert run(record()).unknown_companies == []
