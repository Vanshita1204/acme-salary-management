from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import engine, get_db
from app.main import app
from app.models import Department, JobLevel, JobTitle
from app.seed.org_structure import load_org_structure

BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def migrated_db() -> None:
    """Bring the docker-compose Postgres up to the latest migration once per run."""
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")


@pytest.fixture
def db(migrated_db: None) -> Generator[Session, None, None]:
    """A session whose work is always rolled back, so tests never leave rows behind."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    """API client whose requests run inside the test's rolled-back transaction."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def org(db: Session) -> dict:
    """The seeded departments, titles and levels by name, plus a default role's ids."""
    load_org_structure(db)
    db.flush()
    departments = dict(db.execute(select(Department.name, Department.id)).all())
    titles = dict(db.execute(select(JobTitle.name, JobTitle.id)).all())
    levels = dict(db.execute(select(JobLevel.code, JobLevel.id)).all())
    return {
        "departments": departments,
        "titles": titles,
        "levels": levels,
        "role": {
            "department_id": departments["Engineering"],
            "job_title_id": titles["Software Engineer"],
            "job_level_id": levels["L3"],
        },
    }
