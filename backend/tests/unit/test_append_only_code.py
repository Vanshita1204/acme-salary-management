"""No code path changes or removes compensation history, and the API has no way to ask.

The database enforces this (triggers, privileges); these checks make it fail in review
and CI as well, before anything runs.
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.main import app

APP = Path(__file__).resolve().parents[2] / "app"
MUTATORS = {"update", "delete"}
SQL_AGAINST_HISTORY = re.compile(
    r"\b(update\s+compensation_records|delete\s+from\s+compensation_records|truncate\b[^;\"']*compensation_records)",
    re.IGNORECASE,
)


def app_sources() -> list[Path]:
    return [path for path in APP.rglob("*.py")]


def test_there_are_sources_to_scan():
    assert len(app_sources()) > 40


@pytest.mark.parametrize("path", app_sources(), ids=lambda p: str(p.relative_to(APP)))
def test_no_statement_updates_or_deletes_history(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        # update(CompensationRecord ...) / delete(CompensationRecord ...)
        if isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", "")
            )
            if (
                name in MUTATORS
                and node.args
                and "CompensationRecord" in ast.dump(node.args[0])
            ):
                pytest.fail(f"{path}:{node.lineno} {name}() on CompensationRecord")
            # session.delete(anything): the app never hard-deletes
            if name == "delete" and isinstance(node.func, ast.Attribute):
                pytest.fail(
                    f"{path}:{node.lineno} .delete() call: the app never deletes rows"
                )
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and SQL_AGAINST_HISTORY.search(node.value)
        ):
            pytest.fail(f"{path}:{node.lineno} raw SQL changes compensation_records")


def test_a_record_can_only_be_read_or_added_through_the_api():
    for route in app.routes:
        methods = set(getattr(route, "methods", None) or [])
        path = getattr(route, "path", "")
        if "compensation" in path:
            assert methods <= {"GET", "POST", "HEAD"}, f"{sorted(methods)} {path}"


def test_nothing_in_the_api_can_delete_at_all():
    for route in app.routes:
        assert "DELETE" not in set(getattr(route, "methods", None) or []), route.path


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"DATABASE_URL": "postgresql+psycopg://owner@h/db"}, "owner"),
        (
            {
                "DATABASE_URL": "postgresql+psycopg://owner@h/db",
                "RUNTIME_DATABASE_URL": "postgresql+psycopg://acme_app@h/db",
            },
            "acme_app",
        ),
    ],
)
def test_the_api_connects_as_the_runtime_role_when_one_is_set(env, expected):
    clean = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DATABASE_URL", "RUNTIME_DATABASE_URL"}
    }
    code = "from app.db.session import engine; print(engine.url.username)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={
            **clean,
            "EXCHANGE_RATE_API_URL": "http://x",
            "PYTHONPATH": str(APP.parent),
            **env,
        },
        capture_output=True,
        check=False,
        text=True,
        cwd=APP.parent,
    )
    assert result.stdout.strip() == expected, result.stderr
