"""E6 — large CSV import: the application's in-memory path vs COPY into a staging table.

Usage (from backend/, after `python -m scale_lab.build --size s|m`):
    python -m scale_lab.e6_import --database acme_lab_m --rows 100000 1000000

Hypothesis (stated before running): the application parses, validates and builds every row
as Python objects, so its time and memory grow linearly with the file (and the 10,000-row
cap exists for that reason). Streaming the file with COPY into a staging table, validating
with set-based SQL and inserting with three INSERT ... SELECT statements keeps the Python
process flat (a chunk at a time) and should be several times faster, with the database's
per-row record trigger as the floor.

Both paths run the whole job — parse/stage, validate, insert employees, base-pay records and
current compensation — in one transaction, then roll back so the lab dataset is unchanged
(commit cost is a WAL flush, small next to the work measured). Each path runs in its own
process so its peak memory (ru_maxrss) is its own. The staging path's validation is SQL
equivalents of the application's rules; the email check is a pattern, not the full
`email-validator` rules, which is a real difference to weigh.

The application's path is only run up to its 10,000-row cap.
"""

import argparse
import csv
import json
import random
import resource
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from scale_lab import lab

COLUMNS = [
    "first_name",
    "last_name",
    "email",
    "company",
    "department",
    "job_title",
    "job_level",
    "country",
    "hire_date",
    "currency",
    "base_pay_amount",
]
COPY = (
    "COPY import_staging (first_name, last_name, email, company, department, job_title, "
    "job_level, country, hire_date, currency, base_pay_amount) FROM STDIN WITH (FORMAT csv, HEADER true)"
)

STAGING = """
    CREATE TEMP TABLE import_staging (
        row_no bigint GENERATED ALWAYS AS IDENTITY,
        first_name text, last_name text, email text, company text, department text,
        job_title text, job_level text, country text, hire_date text, currency text,
        base_pay_amount text
    ) ON COMMIT DROP;
    CREATE FUNCTION pg_temp.try_date(value text) RETURNS date LANGUAGE plpgsql IMMUTABLE AS $$
        BEGIN RETURN value::date; EXCEPTION WHEN others THEN RETURN NULL; END $$;
"""
# One row per problem: (row, column, reason), like the application's report.
VALIDATE = """
    SELECT s.row_no, c.col, c.reason FROM (
        SELECT *, count(*) OVER (PARTITION BY lower(email)) AS email_count FROM import_staging
    ) s
    LEFT JOIN companies co ON co.name = s.company
    LEFT JOIN departments d ON d.name = s.department
    LEFT JOIN job_titles t ON t.name = s.job_title
    LEFT JOIN job_levels l ON l.code = s.job_level
    LEFT JOIN countries ct ON ct.code = upper(s.country)
    LEFT JOIN currencies cu ON cu.code = upper(s.currency)
    LEFT JOIN employees e ON e.email = s.email
    CROSS JOIN LATERAL (VALUES
        ('email', 'invalid email address', s.email !~ '^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$'),
        ('email', 'an employee with this email already exists', e.id IS NOT NULL),
        ('email', 'email also appears on another row',
            s.email_count > 1),
        ('company', 'unknown company', co.id IS NULL),
        ('department', 'unknown department', d.id IS NULL),
        ('job_title', 'unknown job title', t.id IS NULL),
        ('job_level', 'unknown job level', l.id IS NULL),
        ('country', 'unknown country', ct.code IS NULL),
        ('currency', 'unsupported currency', cu.code IS NULL),
        ('hire_date', 'invalid date, expected YYYY-MM-DD',
            s.hire_date !~ '^\\d{4}-\\d{2}-\\d{2}$' OR pg_temp.try_date(s.hire_date) IS NULL),
        ('base_pay_amount', 'invalid amount, at most 2 decimals, greater than zero',
            s.base_pay_amount !~ '^\\d+(\\.\\d{1,2})?$' OR s.base_pay_amount::numeric <= 0)
    ) AS c(col, reason, bad) WHERE c.bad
"""
INSERT_EMPLOYEES = """
    INSERT INTO employees (company_id, first_name, last_name, email, department_id,
                           job_title_id, job_level_id, current_country, currency, status, hire_date)
    SELECT co.id, s.first_name, s.last_name, s.email, d.id, t.id, l.id,
           upper(s.country), upper(s.currency), 'active', s.hire_date::date
    FROM import_staging s
    JOIN companies co ON co.name = s.company JOIN departments d ON d.name = s.department
    JOIN job_titles t ON t.name = s.job_title JOIN job_levels l ON l.code = s.job_level
    ORDER BY s.row_no
"""
INSERT_RECORDS = """
    INSERT INTO compensation_records (employee_id, compensation_type_id, change_reason_id,
        effective_date, amount, country, currency, changed_by)
    SELECT e.id, (SELECT id FROM compensation_types WHERE is_base_pay),
           (SELECT id FROM change_reasons WHERE code = 'new_hire'),
           e.hire_date, s.base_pay_amount::numeric, e.current_country, e.currency, 'lab'
    FROM import_staging s JOIN employees e ON e.email = s.email
"""
INSERT_CURRENT = """
    INSERT INTO current_compensation (employee_id, compensation_type_id,
        compensation_record_id, effective_date, amount)
    SELECT r.employee_id, r.compensation_type_id, r.id, r.effective_date, r.amount
    FROM compensation_records r
    WHERE r.employee_id > %(max_id)s AND r.changed_by = 'lab' AND r.effective_date <= current_date
"""


def make_csv(path: Path, rows: int, conn, bad_rows: int = 0) -> None:
    """A deterministic file of valid rows (plus `bad_rows` invalid ones at the end)."""
    rng = random.Random(1204)
    pick = lambda sql: [r[0] for r in conn.execute(sql).fetchall()]
    companies = pick("SELECT name FROM companies ORDER BY id LIMIT 20")
    departments = pick("SELECT name FROM departments ORDER BY id")
    titles = pick("SELECT name FROM job_titles ORDER BY id")
    levels = pick("SELECT code FROM job_levels ORDER BY id")
    countries = pick(
        "SELECT code FROM countries WHERE code IN ('US','IN','GB','DE','SG')"
    )
    currency_of = dict(
        conn.execute("SELECT code, default_currency FROM countries").fetchall()
    )
    start = date(2018, 1, 1)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for n in range(rows + bad_rows):
            country = rng.choice(countries)
            bad = n >= rows
            writer.writerow(
                [
                    f"First{n}",
                    f"Last{n}",
                    f"import.{n}@lab.example" if not bad else f"not-an-email-{n}",
                    rng.choice(companies),
                    rng.choice(departments),
                    rng.choice(titles),
                    rng.choice(levels),
                    country,
                    (start + timedelta(days=rng.randrange(2800))).isoformat(),
                    currency_of[country],
                    f"{rng.randrange(30_000, 200_000)}.00",
                ]
            )


def run_staging(database: str, path: str, work_mem: str | None = None) -> dict:
    started = time.perf_counter()
    marks = {}
    with lab.connect(database) as conn:
        conn.autocommit = False
        conn.execute("SELECT now()")  # starts the transaction
        max_id = conn.execute("SELECT max(id) FROM employees").fetchone()[0]
        if work_mem:
            conn.execute(f"SET LOCAL work_mem = '{work_mem}'")
        conn.execute(STAGING)
        with (
            conn.cursor() as cursor,
            cursor.copy(COPY) as copy,
            open(path, "rb") as source,
        ):
            while chunk := source.read(1 << 20):
                copy.write(chunk)
        # Temp tables are never auto-analyzed; without statistics the planner guessed a
        # tiny table and picked nested loops (validation took 5x longer at 100k rows).
        conn.execute("ANALYZE import_staging")
        marks["stage"] = time.perf_counter() - started
        problems = conn.execute(VALIDATE + " LIMIT 1000").fetchall()
        marks["validate"] = time.perf_counter() - started
        if problems:
            conn.rollback()
            return {
                "outcome": "rejected",
                "problems_reported": len(problems),
                "sample": [list(p) for p in problems[:3]],
                "seconds": round(time.perf_counter() - started, 2),
                "stage_seconds": round(marks["stage"], 2),
                "validate_seconds": round(marks["validate"] - marks["stage"], 2),
            }
        conn.execute(INSERT_EMPLOYEES)
        marks["employees"] = time.perf_counter() - started
        conn.execute(INSERT_RECORDS)
        marks["records"] = time.perf_counter() - started
        conn.execute(INSERT_CURRENT, {"max_id": max_id})
        marks["current"] = time.perf_counter() - started
        inserted = conn.execute(
            "SELECT count(*) FROM employees WHERE email LIKE 'import.%@lab.example'"
        ).fetchone()[0]
        conn.rollback()
    total = time.perf_counter() - started
    return {
        "outcome": "imported (rolled back for the lab)",
        "employees_inserted": inserted,
        "seconds": round(total, 2),
        "stage_seconds": round(marks["stage"], 2),
        "validate_seconds": round(marks["validate"] - marks["stage"], 2),
        "employees_seconds": round(marks["employees"] - marks["validate"], 2),
        "records_seconds": round(marks["records"] - marks["employees"], 2),
        "current_seconds": round(marks["current"] - marks["records"], 2),
    }


def run_app(database: str, path: str) -> dict:
    """The application's own import service on the same file."""
    import os

    os.environ["DATABASE_URL"] = lab.lab_url(database)
    from app.core.config import get_settings

    get_settings.cache_clear()
    from sqlalchemy.orm import Session

    from app.db.session import engine
    from app.services.imports import confirm_file

    started = time.perf_counter()
    content = Path(path).read_bytes()
    with Session(engine) as session:
        result, ids = confirm_file(session, content, "lab")
        session.rollback()
    return {
        "outcome": "imported (rolled back for the lab)" if result.ok else "rejected",
        "employees_inserted": len(ids),
        "seconds": round(time.perf_counter() - started, 2),
    }


def worker(kind: str, database: str, path: str, work_mem: str | None) -> None:
    if kind == "staging":
        result = run_staging(database, path, work_mem)
    else:
        result = run_app(database, path)
    result["peak_rss_mb"] = round(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    )
    print("RESULT " + json.dumps(result))


def spawn(kind: str, database: str, path: Path, work_mem: str | None = None) -> dict:
    extra = ["--work-mem", work_mem] if work_mem else []
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "scale_lab.e6_import",
            "--worker",
            kind,
            "--database",
            database,
            "--file",
            str(path),
            *extra,
        ],
        cwd=lab.BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line for line in out.stdout.splitlines() if line.startswith("RESULT ")]
    if out.returncode or not lines:
        raise RuntimeError(f"{kind} worker failed:\n{out.stdout}\n{out.stderr}")
    return json.loads(lines[-1][len("RESULT ") :])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--rows", type=int, nargs="*", default=[])
    parser.add_argument("--worker", choices=["staging", "app"])
    parser.add_argument("--file")
    parser.add_argument(
        "--work-mem", help="staging path only: SET LOCAL work_mem for the transaction"
    )
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.database, args.file, args.work_mem)
        return

    results = {}
    with lab.connect(args.database) as conn, tempfile.TemporaryDirectory() as tmp:
        before = conn.execute("SELECT count(*) FROM employees").fetchone()[0]
        for rows in args.rows:
            path = Path(tmp) / f"import_{rows}.csv"
            make_csv(path, rows, conn)
            entry = {"file_mb": round(path.stat().st_size / 2**20, 1)}
            entry["staging"] = spawn("staging", args.database, path)
            lab.log(f"E6 {rows:>9,} rows staging: {entry['staging']}")
            if rows >= 100_000:
                entry["staging_work_mem_256mb"] = spawn(
                    "staging", args.database, path, "256MB"
                )
                lab.log(
                    f"E6 {rows:>9,} rows staging, work_mem 256MB: {entry['staging_work_mem_256mb']}"
                )
            if rows <= 10_000:
                entry["application"] = spawn("app", args.database, path)
                lab.log(f"E6 {rows:>9,} rows application: {entry['application']}")
            results[str(rows)] = entry
        bad = Path(tmp) / "bad.csv"
        make_csv(bad, args.rows[-1] if args.rows else 10_000, conn, bad_rows=3)
        results["rejected_file"] = spawn("staging", args.database, bad)
        lab.log(f"E6 file with 3 bad rows: {results['rejected_file']}")
        after = conn.execute("SELECT count(*) FROM employees").fetchone()[0]

    lab.save(
        f"e6_import_{args.database}",
        {
            "database": args.database,
            "employees_before": before,
            "employees_after": after,
            "results": results,
        },
    )


if __name__ == "__main__":
    main()
