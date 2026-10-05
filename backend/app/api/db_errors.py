"""Turn database constraint violations into clean HTTP errors.

Business rules live in the database (constraints and triggers), so the API lets the
write happen and translates a rejection into a 409/422. Messages are built from the
error and the model metadata — the violated constraint's table, columns or referenced
table — so new tables and constraints get readable errors without registering anything.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache

import psycopg
from fastapi import HTTPException, status
from psycopg import errors as pg
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.session import Base

UNIQUE_MESSAGE = "{entity} with this {fields} already exists"
FOREIGN_KEY_MESSAGE = "{entity} does not exist"
CHECK_MESSAGE = "{entity} rule violated: {rule}"
NOT_NULL_MESSAGE = "{field} is required"
FALLBACK_MESSAGE = "request conflicts with stored data"

CHECK_PREFIX = "chk_"  # naming convention for check constraints in the models

# Rule violations the API reports as client errors; anything else is a real 500.
RULE_VIOLATIONS = (psycopg.IntegrityError, pg.RaiseException)


def entity(table_name: str) -> str:
    """'compensation_types' -> 'compensation type', 'companies' -> 'company'."""
    words = table_name.split("_")
    last = words[-1]
    if last.endswith("ies"):
        words[-1] = last[:-3] + "y"
    elif last.endswith("s"):
        words[-1] = last[:-1]
    return " ".join(words)


def humanize(name: str) -> str:
    return name.replace("_", " ")


def join_fields(columns: list[str]) -> str:
    return " and ".join(humanize(c) for c in columns)


@cache
def constraints_by_name() -> dict[str, object]:
    """Every named constraint and index across the models, keyed by its DB name."""
    found: dict[str, object] = {}
    for table in Base.metadata.tables.values():
        for item in (*table.constraints, *table.indexes):
            if item.name:
                found[str(item.name)] = item
    return found


def message_for(exc: DBAPIError) -> str:
    error = exc.orig
    diag = error.diag
    if isinstance(error, pg.RaiseException):
        return diag.message_primary  # trigger messages are already written for humans
    if isinstance(error, pg.NotNullViolation):
        return NOT_NULL_MESSAGE.format(field=humanize(diag.column_name))

    constraint = constraints_by_name().get(diag.constraint_name or "")
    if isinstance(constraint, UniqueConstraint | Index):
        columns = [c.name for c in constraint.columns]
        if columns:
            return UNIQUE_MESSAGE.format(
                entity=entity(diag.table_name), fields=join_fields(columns)
            )
    if isinstance(constraint, ForeignKeyConstraint):
        return FOREIGN_KEY_MESSAGE.format(entity=entity(constraint.referred_table.name))
    if isinstance(constraint, CheckConstraint):
        rule = str(constraint.name).removeprefix(CHECK_PREFIX)
        return CHECK_MESSAGE.format(entity=entity(diag.table_name), rule=humanize(rule))
    return FALLBACK_MESSAGE


def http_error(exc: DBAPIError) -> HTTPException:
    code = (
        status.HTTP_409_CONFLICT
        if isinstance(exc.orig, pg.UniqueViolation)
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(code, message_for(exc))


@contextmanager
def translate_db_errors(db: Session) -> Iterator[None]:
    """Wrap a write so it's all-or-nothing.

    Any failure rolls back everything written inside the block. A constraint or trigger
    rejection becomes a 409/422; anything else is re-raised unchanged.
    """
    try:
        yield
    except DBAPIError as exc:
        db.rollback()
        if not isinstance(exc.orig, RULE_VIOLATIONS):
            raise  # connection loss, syntax errors etc. are real 500s
        raise http_error(exc) from exc
    except Exception:
        db.rollback()  # e.g. a ServiceError after earlier rows were flushed
        raise
