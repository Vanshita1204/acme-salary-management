"""Scale Lab plumbing: throwaway lab databases, timing, and result files.

Lab databases live next to the dev database on the docker-compose Postgres (named
`acme_lab_*`) and are built with the application's own migrations and seed scripts,
run as subprocesses pointed at the lab database via DATABASE_URL.
"""

import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url

from app.core.config import get_settings

BACKEND = Path(__file__).resolve().parents[1]
RESULTS = Path(__file__).resolve().parent / "results"
WARMUP_RUNS = 1
TIMED_RUNS = 5
AS_OF = "2026-10-01"  # fixed, so every lab dataset is identical across runs


def lab_url(database: str) -> str:
    """SQLAlchemy URL for a lab database on the same server as the dev database."""
    return (
        make_url(get_settings().database_url)
        .set(database=database)
        .render_as_string(hide_password=False)
    )


def libpq_url(database: str) -> str:
    return lab_url(database).replace("postgresql+psycopg://", "postgresql://")


def admin_execute(sql: str) -> None:
    """Run a statement that can't be in a transaction (CREATE/DROP DATABASE)."""
    with psycopg.connect(libpq_url("postgres"), autocommit=True) as conn:
        conn.execute(sql)


def recreate_database(database: str) -> None:
    admin_execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    admin_execute(f'CREATE DATABASE "{database}"')


def run_app(database: str, *args: str) -> str:
    """Run an app command (alembic, a seed module) against a lab database."""
    env = {**os.environ, "DATABASE_URL": lab_url(database)}
    result = subprocess.run(
        args, cwd=BACKEND, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def python(*module_args: str) -> tuple[str, ...]:
    return (sys.executable, "-m", *module_args)


def prepare_reference(database: str) -> None:
    """Fresh schema plus the Phase 1.2 reference data, ready for an employee seed."""
    recreate_database(database)
    run_app(database, sys.executable, "-m", "alembic", "upgrade", "head")
    for module in ("reference", "companies", "compensation_types", "change_reasons"):
        run_app(database, *python(f"app.seed.{module}"))


def seed(database: str, count: int, method: str = "batch") -> float:
    """Seed employees and return wall-clock seconds."""
    started = time.perf_counter()
    run_app(
        database,
        *python(
            "app.seed.employees",
            "--count",
            str(count),
            "--as-of",
            AS_OF,
            "--method",
            method,
        ),
    )
    return time.perf_counter() - started


def connect(database: str) -> psycopg.Connection:
    return psycopg.connect(libpq_url(database), autocommit=True)


def analyze(database: str) -> None:
    with connect(database) as conn:
        conn.execute("ANALYZE")


def time_query(conn: psycopg.Connection, sql: str, params: dict | None = None) -> dict:
    """Median of TIMED_RUNS executions after WARMUP_RUNS, plus the plan of one run."""
    for _ in range(WARMUP_RUNS):
        conn.execute(sql, params).fetchall()
    samples = []
    for _ in range(TIMED_RUNS):
        started = time.perf_counter()
        conn.execute(sql, params).fetchall()
        samples.append((time.perf_counter() - started) * 1000)
    plan_rows = conn.execute(
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}", params
    ).fetchall()
    plan = [row[0] for row in plan_rows]
    return {
        "median_ms": round(statistics.median(samples), 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
        "plan": plan,
    }


def table_sizes(conn: psycopg.Connection) -> dict[str, dict]:
    """On-disk size and *estimated* row count (pg_stat n_live_tup) — use count(*) for exact."""
    rows = conn.execute(
        """
        SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid))
        FROM pg_stat_user_tables
        WHERE relname IN ('employees', 'compensation_records', 'current_compensation')
        """
    ).fetchall()
    return {name: {"rows": rows_, "size": size} for name, rows_, size in rows}


def environment() -> dict:
    with connect("postgres") as conn:
        settings = dict(
            conn.execute(
                "SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name IN "
                "('shared_buffers', 'work_mem', 'effective_cache_size', 'max_parallel_workers_per_gather')"
            ).fetchall()
        )
        version = conn.execute("SHOW server_version").fetchone()[0]
    return {
        "machine": platform.platform(),
        "cpu_count": os.cpu_count(),
        "memory_gb": round(
            os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
        ),
        "postgres": version,
        "postgres_settings": settings,
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def save(name: str, payload: dict) -> Path:
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{name}.json"
    path.write_text(
        json.dumps({"environment": environment(), **payload}, indent=2, default=str)
    )
    return path


def log(message: str) -> None:
    print(f"[{datetime.now().astimezone():%H:%M:%S}] {message}", flush=True)


def timed(label: str, action: Callable[[], object]) -> float:
    started = time.perf_counter()
    action()
    elapsed = time.perf_counter() - started
    log(f"{label}: {elapsed:.1f}s")
    return elapsed
