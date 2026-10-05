"""Phase 15: what a hosted instance needs that local development doesn't."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.web import create_site


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("postgresql://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("postgresql+psycopg://u@host/db", "postgresql+psycopg://u@host/db"),
    ],
)
def test_hosted_database_urls_get_the_psycopg_driver(given, expected):
    assert Settings(database_url=given).database_url == expected
    assert (
        Settings(database_url=expected, runtime_database_url=given).runtime_database_url
        == expected
    )


def test_empty_runtime_url_means_unset(monkeypatch):
    assert (
        Settings(
            database_url="postgresql://x/y", runtime_database_url=""
        ).runtime_database_url
        is None
    )


def test_rate_provider_defaults_to_the_one_the_app_was_built_for():
    assert (
        "open.er-api.com"
        in Settings(database_url="postgresql://x/y").exchange_rate_api_url
    )


@pytest.fixture
def dist(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "index.html").write_text("<!doctype html><title>ACME</title>")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    # A file next to dist that must never be served.
    (tmp_path.parent / "secret.txt").write_text("secret")
    return tmp_path


@pytest.fixture
def site(dist):
    return TestClient(create_site(dist))


def test_root_and_client_side_routes_get_the_app(site):
    for path in ("/", "/employees/42", "/analytics", "/import"):
        response = site.get(path)
        assert response.status_code == 200
        assert "<title>ACME</title>" in response.text
        assert response.headers["cache-control"] == "no-cache"


def test_built_files_are_served_as_they_are(site):
    assert site.get("/assets/app.js").text == "console.log(1)"
    assert site.get("/favicon.svg").text == "<svg/>"


def test_the_api_lives_under_api_and_the_platform_health_check_does_not(site):
    assert site.get("/health").json() == {"status": "ok"}
    assert site.get("/api/health").json() == {"status": "ok"}


def test_an_unknown_api_path_is_a_json_404_not_the_app(site):
    response = site.get("/api/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "path", ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"]
)
def test_paths_cannot_leave_the_built_directory(site, path):
    assert "secret" not in site.get(path).text


def test_without_a_build_the_site_is_the_api_alone(tmp_path):
    site = TestClient(create_site(tmp_path / "missing"))
    assert site.get("/api/health").status_code == 200
    assert site.get("/").status_code == 404
