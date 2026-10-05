"""Least-privilege database role for the running application.

The app never changes or removes compensation history, so the role it runs as shouldn't
be *able* to: `compensation_records` gets SELECT and INSERT only. The append-only trigger
(and the TRUNCATE trigger) still guard the table, but a role without the privileges
can't disable them either, and a bug can't reach around them.

Migrations, seeds and this command run as the owner (`DATABASE_URL`); the API runs as the
role created here (`RUNTIME_DATABASE_URL`). Usage, from backend/:

    APP_DB_PASSWORD=... python -m app.db.grants acme_app

Safe to re-run: it creates the role if missing, then (re)applies exactly these grants.
Run it again after a migration adds a table (tests/integration/test_app_role.py fails
until the new table has an entry below).
"""

import argparse
import os
import re
from collections.abc import Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from app.core.config import get_settings

SELECT, INSERT, UPDATE = "SELECT", "INSERT", "UPDATE"

# table -> what the application may do. DELETE and TRUNCATE are granted nowhere: nothing in
# the app deletes (employees are terminated, never removed; reference rows are only added).
TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    # Reference data: read, add. (Departments, titles and levels can also be renamed.)
    "currencies": (SELECT, INSERT),
    "countries": (SELECT, INSERT),
    "companies": (SELECT, INSERT),
    "compensation_types": (SELECT, INSERT),
    "change_reasons": (SELECT, INSERT),
    "departments": (SELECT, INSERT, UPDATE),
    "job_titles": (SELECT, INSERT, UPDATE),
    "job_levels": (SELECT, INSERT, UPDATE),
    # People: edited and terminated, never removed.
    "employees": (SELECT, INSERT, UPDATE),
    # History: append-only. This is the point of the role.
    "compensation_records": (SELECT, INSERT),
    # The "current" pointers move as records arrive.
    "current_compensation": (SELECT, INSERT, UPDATE),
    # Daily refresh upserts today's rates.
    "exchange_rates": (SELECT, INSERT, UPDATE),
}
# Sequences the app's inserts draw from directly (identity columns need no grant).
SEQUENCES = ("employee_code_seq",)

ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def check_role_name(role: str) -> str:
    if not ROLE_PATTERN.fullmatch(role):
        raise ValueError(
            f"invalid role name {role!r}: use lowercase letters, digits and underscores"
        )
    return role


def grant_statements(role: str) -> list[str]:
    """Every statement that sets the role's privileges: first take everything away, then give back."""
    role = check_role_name(role)
    statements = [
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}",
        f"GRANT USAGE ON SCHEMA public TO {role}",
    ]
    for table, privileges in TABLE_PRIVILEGES.items():
        statements.append(f"GRANT {', '.join(privileges)} ON {table} TO {role}")
    statements += [
        f"GRANT USAGE ON SEQUENCE {sequence} TO {role}" for sequence in SEQUENCES
    ]
    return statements


def apply_grants(connection: Connection, role: str) -> None:
    for statement in grant_statements(role):
        connection.exec_driver_sql(statement)


def create_role(
    connection: Connection, role: str, password: str | None, *, login: bool = True
) -> None:
    """Create the role if it doesn't exist. A role that can log in needs a password."""
    role = check_role_name(role)
    exists = connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    )
    if exists:
        return
    if login and not password:
        raise ValueError("a password is required to create a role that can log in")
    options = "LOGIN PASSWORD :password" if login else "NOLOGIN"
    connection.execute(
        text(f"CREATE ROLE {role} {options} NOSUPERUSER NOCREATEDB NOCREATEROLE"),
        {"password": password} if login else {},
    )


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Create the application's least-privilege database role."
    )
    parser.add_argument("role", help="role name, e.g. acme_app")
    args = parser.parse_args(list(argv) if argv is not None else None)
    password = os.environ.get("APP_DB_PASSWORD")

    engine = create_engine(get_settings().database_url)
    with engine.begin() as connection:
        create_role(connection, args.role, password)
        apply_grants(connection, args.role)
    print(
        f"Role {args.role!r} can read and add, but never change or remove, compensation history."
    )
    print(
        "Set RUNTIME_DATABASE_URL to a URL for this role; keep DATABASE_URL (the owner) for migrations and seeds."
    )


if __name__ == "__main__":
    main()
