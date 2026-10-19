import time
from collections.abc import Generator
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import Engine, MetaData, create_engine, event, exc
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()
# pool_pre_ping: test a pooled connection before using it, so after a database restart the
# first request replaces the dead connection instead of failing.
# prepare_threshold=None: psycopg 3 otherwise prepares a statement after 5 uses, and Postgres
# can then switch to a generic plan that doesn't know the parameter values. That took the
# future-dated promotion that runs on every read from 1.4 ms to over 150 ms after about ten
# requests on a connection (Scale Lab E4, E10).
engine = create_engine(
    _settings.runtime_database_url or _settings.database_url,
    future=True,
    # Ping on every checkout only when asked to (db_ping_idle_seconds = 0); otherwise the
    # idle-only ping below. Either way a dead connection is replaced rather than used.
    pool_pre_ping=_settings.db_ping_idle_seconds <= 0,
    connect_args={"prepare_threshold": None},
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_timeout=_settings.db_pool_timeout,
)


def install_idle_ping(target: Engine, idle_seconds: float) -> None:
    """Test a pooled connection with a ping only if it has sat idle for `idle_seconds`, and
    replace it if it has died. A ping is a round trip on every request; skipping it while a
    connection is in regular use saves that round trip (Scale Lab E24)."""

    @event.listens_for(target, "checkin")
    def remember_last_use(dbapi_connection, connection_record):
        connection_record.info["last_used"] = time.monotonic()

    @event.listens_for(target, "checkout")
    def ping_if_idle(dbapi_connection, connection_record, connection_proxy):
        last_used = connection_record.info.get("last_used")
        if last_used is None or time.monotonic() - last_used < idle_seconds:
            return
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            dbapi_connection.rollback()
        except Exception as error:
            raise exc.DisconnectionError("the pooled connection is no longer usable") from error


if _settings.db_ping_idle_seconds > 0:
    install_idle_ping(engine, _settings.db_ping_idle_seconds)


@dataclass
class QueryStats:
    """What one request spent in the database, for the `Server-Timing` header."""

    count: int = 0
    total_ms: float = 0.0
    slowest_ms: float = 0.0


# Set per request by the timing middleware. The object is shared with the worker thread
# that runs a sync endpoint, so the counts accumulate there and are read afterwards.
query_stats: ContextVar[QueryStats | None] = ContextVar("query_stats", default=None)


@event.listens_for(engine, "before_cursor_execute")
def _query_started(conn, cursor, statement, parameters, context, executemany):
    conn.info["query_started"] = time.perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _query_finished(conn, cursor, statement, parameters, context, executemany):
    stats = query_stats.get()
    started = conn.info.pop("query_started", None)
    if stats is not None and started is not None:
        elapsed = (time.perf_counter() - started) * 1000
        stats.count += 1
        stats.total_ms += elapsed
        stats.slowest_ms = max(stats.slowest_ms, elapsed)


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    # Postgres's own default constraint names, so unnamed model constraints carry the
    # same name as in the database (app.api.db_errors looks constraints up by name).
    metadata = MetaData(
        naming_convention={
            "pk": "%(table_name)s_pkey",
            "uq": "%(table_name)s_%(column_0_N_name)s_key",
            "fk": "%(table_name)s_%(column_0_N_name)s_fkey",
        }
    )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
