from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The owner: migrations, seeds, scripts and tests.
    database_url: str
    # Optional: the API connects as this least-privilege role instead (see app.db.grants),
    # so it can't change or remove compensation history even by mistake.
    runtime_database_url: str | None = None
    # Latest-rates endpoint returning units of each currency per 1 USD (open.er-api.com format).
    exchange_rate_api_url: str = "https://open.er-api.com/v6/latest/USD"
    environment: str = "development"
    # Connection pool for the API (SQLAlchemy's defaults). The pool serves at most
    # pool_size + max_overflow requests at once; the rest wait up to pool_timeout seconds.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: float = 30
    # Pay-insight views read each employee's total from the `employee_totals` materialized
    # view (fast, as fresh as its last refresh) instead of aggregating all current pay per
    # request (always current). Off by default in tests, which check the live semantics.
    analytics_totals_view: bool = True
    # Seconds the API waits after a write before refreshing the view, so a burst of writes
    # (an import, a run of edits) causes one refresh.
    totals_refresh_delay: float = 10
    # How long an identical pay-insight request is answered from memory (0 turns it off).
    analytics_cache_seconds: float = 300

    @field_validator("database_url", "runtime_database_url")
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
