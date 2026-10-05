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
