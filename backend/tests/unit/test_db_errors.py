import pytest

from app.api.db_errors import entity, join_fields


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("companies", "company"),
        ("countries", "country"),
        ("currencies", "currency"),
        ("employees", "employee"),
        ("change_reasons", "change reason"),
        ("compensation_types", "compensation type"),
        ("compensation_records", "compensation record"),
    ],
)
def test_entity_names_from_tables(table, expected):
    assert entity(table) == expected


def test_join_fields():
    assert join_fields(["email"]) == "email"
    assert join_fields(["category", "subtype"]) == "category and subtype"
    assert join_fields(["current_country"]) == "current country"
