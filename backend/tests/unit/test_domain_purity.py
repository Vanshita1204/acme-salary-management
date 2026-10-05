"""Phase 2 'done when': domain modules never import a DB session or an HTTP client."""

import ast
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[2] / "app" / "domain"
FORBIDDEN = (
    "sqlalchemy",
    "psycopg",
    "httpx",
    "requests",
    "fastapi",
    "app.db",
    "app.models",
    "app.services",
)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", sorted(DOMAIN.glob("*.py")), ids=lambda p: p.name)
def test_domain_module_is_pure(path):
    offending = {
        name
        for name in imported_modules(path)
        if any(name == f or name.startswith(f + ".") for f in FORBIDDEN)
    }
    assert offending == set()
