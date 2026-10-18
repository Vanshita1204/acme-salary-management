import time
from collections.abc import Generator
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()
# pool_pre_ping: test a pooled connection before using it, so after a database restart the
# first request replaces the dead connection instead of failing.
engine = create_engine(
    _settings.runtime_database_url or _settings.database_url,
    future=True,
    pool_pre_ping=True,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_timeout=_settings.db_pool_timeout,
)


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
