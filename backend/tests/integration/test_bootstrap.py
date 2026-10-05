"""Phase 15: a brand-new, empty database becomes a working instance, and starting the
container again changes nothing."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests import conftest

BACKEND = Path(__file__).resolve().parents[2]
COUNTS = """
    select (select count(*) from employees), (select count(*) from compensation_records),
           (select count(*) from current_compensation), (select count(*) from currencies),
           (select count(*) from compensation_types), (select count(*) from change_reasons),
           (select count(*) from departments), (select count(*) from companies)
"""


@pytest.fixture
def empty_database():
    base = make_url(conftest.test_database_url())
    name = f"{base.database}_boot_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    yield base.set(database=name)
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def bootstrap(url, employees: int) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATABASE_URL": url.render_as_string(hide_password=False),
        # Nothing listens here: the rate fetch fails fast, like a provider outage.
        "EXCHANGE_RATE_API_URL": "http://127.0.0.1:9/rates",
    }
    env.pop("RUNTIME_DATABASE_URL", None)
    return subprocess.run(
        [sys.executable, "-m", "app.bootstrap", "--employees", str(employees)],
        cwd=BACKEND,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )


def counts(url) -> tuple:
    engine = create_engine(url)
    with engine.connect() as connection:
        row = tuple(connection.execute(text(COUNTS)).one())
    engine.dispose()
    return row


def test_empty_database_becomes_a_seeded_instance_and_a_restart_changes_nothing(
    empty_database,
):
    first = bootstrap(empty_database, employees=25)
    assert first.returncode == 0, first.stdout + first.stderr
    employees, records, current, currencies, types, reasons, departments, companies = (
        counts(empty_database)
    )
    assert employees == 25
    assert records >= employees and current >= employees
    assert min(currencies, types, reasons, departments, companies) > 0
    # The unreachable provider is reported, not fatal.
    assert "exchange rates not fetched" in first.stdout

    second = bootstrap(empty_database, employees=25)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already present" in second.stdout
    assert counts(empty_database) == (
        employees,
        records,
        current,
        currencies,
        types,
        reasons,
        departments,
        companies,
    )
