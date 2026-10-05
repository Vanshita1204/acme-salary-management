"""Test isolation, applied to every test.

1. **Their own database.** Tests run against `<your database>_test`, created on first use,
   never the database the app and your data live in. (The integration tests roll back what
   they write, but a test database means they also can't be broken by, or break, real
   data: a leftover relocation record in the dev database used to fail a test.) Set
   TEST_DATABASE_URL to use a specific database instead; pointing it at the app's own
   database is allowed but has to be explicit.
2. **No network.** Tests use fixed exchange rates and a faked provider, never the live one
   (REQUIREMENTS §7). Any attempt to leave the machine fails the test.
"""

import os
import socket
import sys
from ipaddress import ip_address

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings


def test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    app_url = make_url(get_settings().database_url)
    return app_url.set(database=f"{app_url.database}_test").render_as_string(
        hide_password=False
    )


def ensure_database(url: str) -> None:
    """Create the test database if it doesn't exist yet. Failing here is not fatal: unit
    tests don't need a database, and integration tests will say what's wrong."""
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if not exists:
                name = target.database.replace('"', '""')
                connection.execute(text(f'CREATE DATABASE "{name}"'))
    except SQLAlchemyError as error:
        print(
            f"\n[tests] couldn't create test database {target.database!r}: {error.__cause__ or error}",
            file=sys.stderr,
        )
    finally:
        admin.dispose()


# Before anything imports the app's engine: point the app at the test database, as the owner.
_URL = test_database_url()
ensure_database(_URL)
os.environ["DATABASE_URL"] = _URL
os.environ.pop("RUNTIME_DATABASE_URL", None)
get_settings.cache_clear()


def is_local(address) -> bool:
    if isinstance(address, str):  # a unix socket path
        return True
    host = address[0]
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@pytest.fixture(autouse=True, scope="session")
def no_network():
    """Nothing in the test run may leave this machine. Guarded at two levels: Python sockets
    (loopback only) and the HTTP client itself, since outbound traffic may go through a proxy
    that is itself on this machine. The database driver (libpq) manages its own sockets and
    is unaffected; a faked provider (`httpx.MockTransport`) is a different class and works."""
    real_connect = socket.socket.connect
    real_send = httpx.HTTPTransport.handle_request
    real_async_send = httpx.AsyncHTTPTransport.handle_async_request

    def guarded_connect(self, address):
        if not is_local(address):
            raise AssertionError(
                f"tests must not use the network (tried to connect to {address[0]})"
            )
        return real_connect(self, address)

    def no_http(self, request):
        raise AssertionError(
            f"tests must not use the network (tried {request.method} {request.url})"
        )

    async def no_async_http(self, request):
        raise AssertionError(
            f"tests must not use the network (tried {request.method} {request.url})"
        )

    socket.socket.connect = guarded_connect
    httpx.HTTPTransport.handle_request = no_http
    httpx.AsyncHTTPTransport.handle_async_request = no_async_http
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        httpx.HTTPTransport.handle_request = real_send
        httpx.AsyncHTTPTransport.handle_async_request = real_async_send
