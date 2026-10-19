"""Defaults are tuned for the 10,000 employees the app is specified for; the features built
for a million turn on only in development, or when asked for explicitly."""

from app.core.config import Settings

URL = "postgresql://user:pw@localhost/db"


def settings(**values) -> Settings:
    # Values passed directly beat the environment and the .env file. The test run sets
    # ANALYTICS_TOTALS_VIEW itself, so "unset" has to be passed explicitly.
    return Settings(database_url=URL, **{"analytics_totals_view": None, **values})


def test_the_default_environment_is_production():
    assert Settings.model_fields["environment"].default == "production"


def test_stored_pay_totals_follow_the_environment():
    assert settings(environment="development").use_totals_view is True
    assert settings(environment="production").use_totals_view is False
    assert settings(environment="staging").use_totals_view is False


def test_stored_pay_totals_can_be_forced_either_way():
    assert settings(
        environment="production", analytics_totals_view=True
    ).use_totals_view
    assert not settings(
        environment="development", analytics_totals_view=False
    ).use_totals_view


def test_defaults_that_are_not_environment_specific_are_the_small_scale_ones():
    defaults = Settings.model_fields
    assert defaults["totals_refresh_delay"].default <= 5  # a new hire shows up quickly
    assert (defaults["db_pool_size"].default, defaults["db_max_overflow"].default) == (
        5,
        10,
    )
