"""The test setup itself: no network, and its own database."""

import os

import httpx
import pytest
from sqlalchemy.engine import make_url

from app.core.config import get_settings


def test_a_real_http_call_fails_instead_of_reaching_the_provider():
    with pytest.raises(Exception, match="must not use the network"):
        httpx.get("https://open.er-api.com/v6/latest/USD", timeout=2)


def test_faked_providers_still_work():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})
        )
    )
    assert client.get("https://open.er-api.com/v6/latest/USD").json() == {"ok": True}


def test_tests_are_pointed_at_a_test_database():
    name = make_url(get_settings().database_url).database
    assert name.endswith("_test") or os.environ.get("TEST_DATABASE_URL"), name
    assert get_settings().runtime_database_url is None  # tests run as the owner
