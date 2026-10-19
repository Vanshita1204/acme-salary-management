"""The pool pings a connection only after it has sat idle (Scale Lab E24)."""

import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.db.session import install_idle_ping


@pytest.fixture
def admin():
    engine = create_engine(get_settings().database_url, isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


def pooled_engine(idle_seconds: float):
    engine = create_engine(get_settings().database_url, pool_size=1, max_overflow=0)
    install_idle_ping(engine, idle_seconds)
    return engine


def backend_pid(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(text("SELECT pg_backend_pid()")).scalar()


def kill(admin, pid: int) -> None:
    with admin.connect() as connection:
        connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    time.sleep(0.05)  # let the server close the socket


def test_a_connection_that_died_while_idle_is_replaced_without_an_error(admin):
    engine = pooled_engine(idle_seconds=0.05)
    first = backend_pid(engine)
    kill(admin, first)
    time.sleep(0.1)  # idle for longer than the window

    second = backend_pid(engine)  # would raise if the dead connection were handed out

    assert second != first
    engine.dispose()


def test_a_connection_in_regular_use_is_not_pinged_so_a_dead_one_fails_once(admin):
    engine = pooled_engine(idle_seconds=60)
    first = backend_pid(engine)
    kill(admin, first)

    with pytest.raises(DBAPIError):  # the cost of skipping the ping, inside the window
        backend_pid(engine)

    assert backend_pid(engine) != first  # the pool discarded the dead connection
    engine.dispose()


def test_a_healthy_idle_connection_is_kept():
    engine = pooled_engine(idle_seconds=0.05)
    first = backend_pid(engine)
    time.sleep(0.1)

    assert backend_pid(engine) == first  # the ping succeeded; no needless reconnect
    engine.dispose()
