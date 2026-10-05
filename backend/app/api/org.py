"""Departments, job titles and job levels (Phase 7A), managed by HR from the UI.

Create, list, get and rename — no delete, because employees point at them. Names are
unique ignoring case, so a case-variant duplicate is a 409 from the database.
"""

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.db_errors import translate_db_errors
from app.api.deps import DbSession, get_or_404
from app.models import Department, JobLevel, JobTitle
from app.schemas.catalog import (
    DepartmentIn,
    DepartmentOut,
    JobLevelIn,
    JobLevelOut,
    JobLevelUpdate,
    JobTitleIn,
    JobTitleOut,
)

departments = APIRouter(prefix="/departments", tags=["org structure"])
job_titles = APIRouter(prefix="/job-titles", tags=["org structure"])
job_levels = APIRouter(prefix="/job-levels", tags=["org structure"])


def save[T](db: Session, obj: T, changes: dict) -> T:
    for name, value in changes.items():
        setattr(obj, name, value)
    with translate_db_errors(db):
        db.add(obj)
        db.commit()
    db.refresh(obj)
    return obj


@departments.get("", response_model=list[DepartmentOut])
def list_departments(db: DbSession) -> list[Department]:
    return list(db.scalars(select(Department).order_by(Department.name)))


@departments.get("/{department_id}", response_model=DepartmentOut)
def get_department(department_id: int, db: DbSession) -> Department:
    return get_or_404(db, Department, department_id, "department")


@departments.post("", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
def create_department(body: DepartmentIn, db: DbSession) -> Department:
    return save(db, Department(), body.model_dump())


@departments.patch("/{department_id}", response_model=DepartmentOut)
def rename_department(
    department_id: int, body: DepartmentIn, db: DbSession
) -> Department:
    department = get_or_404(db, Department, department_id, "department")
    return save(db, department, body.model_dump())


@job_titles.get("", response_model=list[JobTitleOut])
def list_job_titles(db: DbSession) -> list[JobTitle]:
    return list(db.scalars(select(JobTitle).order_by(JobTitle.name)))


@job_titles.get("/{job_title_id}", response_model=JobTitleOut)
def get_job_title(job_title_id: int, db: DbSession) -> JobTitle:
    return get_or_404(db, JobTitle, job_title_id, "job title")


@job_titles.post("", response_model=JobTitleOut, status_code=status.HTTP_201_CREATED)
def create_job_title(body: JobTitleIn, db: DbSession) -> JobTitle:
    return save(db, JobTitle(), body.model_dump())


@job_titles.patch("/{job_title_id}", response_model=JobTitleOut)
def rename_job_title(job_title_id: int, body: JobTitleIn, db: DbSession) -> JobTitle:
    job_title = get_or_404(db, JobTitle, job_title_id, "job title")
    return save(db, job_title, body.model_dump())


@job_levels.get("", response_model=list[JobLevelOut])
def list_job_levels(db: DbSession) -> list[JobLevel]:
    """Most junior first."""
    return list(db.scalars(select(JobLevel).order_by(JobLevel.rank)))


@job_levels.get("/{job_level_id}", response_model=JobLevelOut)
def get_job_level(job_level_id: int, db: DbSession) -> JobLevel:
    return get_or_404(db, JobLevel, job_level_id, "job level")


@job_levels.post("", response_model=JobLevelOut, status_code=status.HTTP_201_CREATED)
def create_job_level(body: JobLevelIn, db: DbSession) -> JobLevel:
    return save(db, JobLevel(), body.model_dump())


@job_levels.patch("/{job_level_id}", response_model=JobLevelOut)
def update_job_level(
    job_level_id: int, body: JobLevelUpdate, db: DbSession
) -> JobLevel:
    job_level = get_or_404(db, JobLevel, job_level_id, "job level")
    return save(db, job_level, body.model_dump(exclude_unset=True))
