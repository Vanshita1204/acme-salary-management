from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The owner: migrations, seeds, scripts and tests.
    database_url: str
    # Optional: the API connects as this least-privilege role instead (see app.db.grants),
    # so it can't change or remove compensation history even by mistake.
    runtime_database_url: str | None = None
    # Latest-rates endpoint returning units of each currency per 1 USD (open.er-api.com format).
    exchange_rate_api_url: str
    environment: str = "development"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
