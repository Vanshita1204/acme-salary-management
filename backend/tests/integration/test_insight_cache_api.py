from datetime import date
from decimal import Decimal

import pytest

from app.main import response_cache
from app.models import ExchangeRate
from app.seed.reference import load_reference_data


@pytest.fixture
def cache_on(db):
    load_reference_data(db)
    db.add(ExchangeRate(currency="USD", rate_to_usd=Decimal(1), rate_date=date(2099, 1, 1), source="pytest"))
    db.flush()
    response_cache.ttl = 300
    response_cache.invalidate()
    yield
    response_cache.ttl = 0
    response_cache.invalidate()


def cache_status(response) -> str:
    return response.headers["server-timing"].split("cache;desc=")[1].strip('"')


def test_an_identical_insight_request_is_answered_from_memory(client, cache_on):
    first = client.get("/analytics/summary")
    second = client.get("/analytics/summary")

    assert (cache_status(first), cache_status(second)) == ("miss", "hit")
    assert second.json() == first.json()
    assert cache_status(client.get("/analytics/summary?country=IN")) == "miss"


def test_a_write_through_the_api_empties_the_cache(client, cache_on):
    client.get("/analytics/summary")
    client.post("/companies", json={"name": "pytest-cache-co"})

    assert cache_status(client.get("/analytics/summary")) == "miss"


def test_other_endpoints_are_never_cached(client, cache_on):
    client.get("/employees")

    assert "cache" not in client.get("/employees").headers["server-timing"]


def test_the_cache_is_off_unless_switched_on(client, cache_on):
    response_cache.ttl = 0
    assert "cache" not in client.get("/analytics/summary").headers["server-timing"]
