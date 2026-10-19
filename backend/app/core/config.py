from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The owner: migrations, seeds, scripts and tests.
    database_url: str
    # Optional: the API connects as this least-privilege role instead (see app.db.grants),
    # so it can't change or remove compensation history even by mistake.
    runtime_database_url: str | None = None
    # The database the tests run against (they never touch `database_url`'s data).
    # Unset: `<database name>_test`, created on first use.
    test_database_url: str | None = None
    # Latest-rates endpoint returning units of each currency per 1 USD (open.er-api.com format).
    exchange_rate_api_url: str = "https://open.er-api.com/v6/latest/USD"
    # "development" turns on the features built for much larger data (see
    # `use_totals_view`); anything else, including this default, is tuned for the
    # 10,000 employees the app is specified for.
    environment: str = "production"
    # Connection pool for the API (SQLAlchemy's defaults). The pool serves at most
    # pool_size + max_overflow requests at once; the rest wait up to pool_timeout seconds.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: float = 30
    # Pay-insight views read each employee's total from the `employee_totals` materialized
    # view (fast at a million employees, as fresh as its last refresh) instead of
    # aggregating all current pay per request (always current; ample at 10,000). Unset, it
    # follows the environment: on in development, off otherwise. Set it to override.
    analytics_totals_view: bool | None = None
    # Seconds the API waits after a write before refreshing the view, so a burst of writes
    # (an import, a run of edits) causes one refresh.
    totals_refresh_delay: float = 2
    # Round trips to the database (Scale Lab E24). A pooled connection is tested with a ping
    # only if it has sat idle this long, instead of on every checkout (0: every checkout, as
    # SQLAlchemy's pool_pre_ping does). After a database restart, a connection used within
    # this window may fail once before the pool replaces it.
    db_ping_idle_seconds: float = 10
    # How long the latest exchange rates are remembered in memory (0: ask every time). A
    # write through the API forgets them at once; a refresh by another process (the daily
    # job) shows up within this time. Known currencies are remembered when this is above 0.
    reference_cache_seconds: float = 60
    # Look for future-dated pay that has come due once per UTC day per process, instead of
    # on every read: nothing new can become due until the date changes.
    promote_once_per_day: bool = True
    # Compress responses with gzip when the browser accepts it (Scale Lab E21). On by
    # default: JSON and the JavaScript bundle shrink 3-10x, which is time on a real network.
    compress_responses: bool = True
    # How long an identical pay-insight request is answered from memory (0 turns it off).
    analytics_cache_seconds: float = 300

    @property
    def use_totals_view(self) -> bool:
        if self.analytics_totals_view is not None:
            return self.analytics_totals_view
        return self.environment == "development"

    @field_validator("database_url", "runtime_database_url", "test_database_url")
    @classmethod
    def use_psycopg_driver(cls, url: str | None) -> str | None:
        """Hosted Postgres hands out `postgres://` or `postgresql://` URLs; the driver
        this app ships with is psycopg 3, which SQLAlchemy names explicitly."""
        if url:
            for scheme in ("postgres://", "postgresql://"):
                if url.startswith(scheme):
                    return "postgresql+psycopg://" + url[len(scheme) :]
        return url or None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
