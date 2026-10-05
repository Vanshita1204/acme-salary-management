"""The employee generator, without a database: determinism and history invariants."""

from datetime import date

import pytest

from app.seed import iso_data
from app.seed.employees import (
    COUNTRIES,
    REASON_CODES,
    TYPE_KEYS,
    Catalog,
    Generator,
    anniversaries,
    ascii_slug,
    quarter_ends,
)

AS_OF = date(2026, 10, 1)
CATALOG = Catalog(
    company_ids=[1, 2, 3],
    currencies={c: iso_data.countries()[c][1] for c in COUNTRIES},
    types={key: i for i, key in enumerate(TYPE_KEYS, start=1)},
    reasons={code: i for i, code in enumerate(REASON_CODES, start=1)},
)
BASE_TYPE = CATALOG.types["base"]
REASON_BY_ID = {v: k for k, v in CATALOG.reasons.items()}


def generate(seed=1204, count=300):
    gen = Generator(CATALOG, seed, AS_OF)
    people = [gen.employee(i) for i in range(1, count + 1)]
    histories = [gen.history(i, facts) for i, (_, facts) in enumerate(people, start=1)]
    return [row for row, _ in people], histories


@pytest.fixture(scope="module")
def sample():
    return generate()


def test_same_seed_produces_identical_data(sample):
    assert generate() == sample


def test_different_seed_produces_different_data(sample):
    assert generate(seed=7)[0] != sample[0]


def test_employee_rows_are_consistent(sample):
    rows, _ = sample
    assert len({r["email"] for r in rows}) == len(rows)
    for row in rows:
        assert row["hire_date"] < AS_OF
        assert (row["status"] == "terminated") == (row["termination_date"] is not None)
        if row["termination_date"]:
            assert row["hire_date"] <= row["termination_date"] <= AS_OF
        assert row["currency"] in {CATALOG.currencies[row["current_country"]], "USD"}


def test_histories_respect_the_database_rules(sample):
    rows, histories = sample
    for row, history in zip(rows, histories, strict=True):
        end = row["termination_date"] or AS_OF
        for record in history:
            assert row["hire_date"] <= record["effective_date"] <= end
            assert record["currency"] == row["currency"]
            assert record["country"] == row["current_country"]
            assert record["amount"] >= 0
            assert record["amount"].as_tuple().exponent >= -2


def test_every_employee_starts_with_a_positive_new_hire_base_pay(sample):
    for row, history in zip(*sample, strict=True):
        base = [r for r in history if r["compensation_type_id"] == BASE_TYPE]
        assert base[0]["effective_date"] == row["hire_date"]
        assert REASON_BY_ID[base[0]["change_reason_id"]] == "new_hire"
        assert all(r["amount"] > 0 for r in base)


def test_base_pay_history_is_chronological(sample):
    for history in sample[1]:
        dates = [
            r["effective_date"]
            for r in history
            if r["compensation_type_id"] == BASE_TYPE
        ]
        assert dates == sorted(dates)


def test_anniversaries_and_quarter_ends_are_exclusive_of_start():
    assert list(anniversaries(date(2024, 4, 1), date(2026, 4, 1), (4, 1))) == [
        date(2025, 4, 1),
        date(2026, 4, 1),
    ]
    assert list(quarter_ends(date(2025, 3, 31), date(2025, 12, 30))) == [
        date(2025, 6, 30),
        date(2025, 9, 30),
    ]


def test_ascii_slug():
    assert ascii_slug("Łukasz") == "ukasz"
    assert ascii_slug("Grégoire") == "gregoire"
    assert ascii_slug("O'Brien-Smith") == "obriensmith"
    assert ascii_slug("岩田") == "employee"
