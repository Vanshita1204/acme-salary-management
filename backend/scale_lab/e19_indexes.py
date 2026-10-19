"""E19: covering, partial and BRIN index variants against the expensive queries (1M employees).

Each candidate is created in the lab database, measured and dropped; the application is not
changed. For every state it times the currency count, the pay-insight views, a date-range
count on the history, and a bulk insert of 100,000 employees, and records whether the
planner used the new index and how big it is.

Usage (from backend/):
    python -m scale_lab.e19_indexes [--database acme_lab_1m]
"""

import argparse
import statistics
import time
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services import analytics as a
from app.services import directory as d
from scale_lab import lab
from scale_lab.e13_trigram import measure_insert

USAGE = """
SELECT e.currency, count(*) FROM employees e
WHERE e.status = 'active' {extra} AND EXISTS (SELECT 1 FROM employee_totals t WHERE t.employee_id = e.id)
GROUP BY e.currency ORDER BY 1"""
FILTER = (
    "AND e.department_id = (SELECT id FROM departments WHERE name = 'Engineering') "
    "AND e.current_country = 'IN'"
)
DATE_RANGE = (
    "SELECT count(*), sum(amount) FROM compensation_records "
    "WHERE effective_date BETWEEN '2026-09-17' AND '2026-10-17'"
)
VIEWS = {
    "summary": lambda s: a.summary(s, d.DirectoryQuery()),
    "stats by department": lambda s: a.grouped_stats(
        s, d.DirectoryQuery(), "department"
    ),
    "role across countries": lambda s: a.role_by_country(s, d.DirectoryQuery()),
    "histogram": lambda s: a.histogram(s, d.DirectoryQuery(), 20),
    "composition": lambda s: a.composition(s, d.DirectoryQuery()),
    "outliers": lambda s: a.outliers(s, d.DirectoryQuery()),
}
CANDIDATES = {
    "I1 partial covering (currency, id) WHERE active": (
        "ix_lab_active_currency",
        "CREATE INDEX ix_lab_active_currency ON employees (currency, id) WHERE status = 'active'",
        "employees",
    ),
    "I2 wide partial covering for the population": (
        "ix_lab_active_population",
        (
            "CREATE INDEX ix_lab_active_population ON employees (id) INCLUDE (company_id, "
            "department_id, job_title_id, job_level_id, current_country, currency) "
            "WHERE status = 'active'"
        ),
        "employees",
    ),
    "I3 BRIN on history effective_date": (
        "ix_lab_history_date_brin",
        "CREATE INDEX ix_lab_history_date_brin ON compensation_records USING brin (effective_date)",
        "compensation_records",
    ),
}


def median_ms(call, runs: int = 5) -> float:
    call()
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 1)


def measure(conn, engine, index: str | None) -> dict:
    out: dict = {}
    plan = "\n".join(
        r[0] for r in conn.execute("EXPLAIN " + USAGE.format(extra="")).fetchall()
    )
    out["currency_count_unfiltered_ms"] = median_ms(
        lambda: conn.execute(USAGE.format(extra="")).fetchall()
    )
    out["currency_count_filtered_ms"] = median_ms(
        lambda: conn.execute(USAGE.format(extra=FILTER)).fetchall()
    )
    out["date_range_count_ms"] = median_ms(lambda: conn.execute(DATE_RANGE).fetchall())
    date_plan = "\n".join(
        r[0] for r in conn.execute("EXPLAIN " + DATE_RANGE).fetchall()
    )
    out["used_by_currency_count"] = bool(index and index in plan)
    out["used_by_date_range"] = bool(index and index in date_plan)
    views = {}
    for name, call in VIEWS.items():
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            with Session(engine) as session:
                call(session)
                session.commit()
            samples.append((time.perf_counter() - started) * 1000)
        views[name] = round(min(samples))
    out["views_ms"] = views
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_lab_1m")
    args = parser.parse_args()
    engine = create_engine(
        lab.lab_url(args.database),
        future=True,
        connect_args={"prepare_threshold": None},
    )
    results: dict = {"as_of": datetime.now(UTC).date().isoformat()}

    def record(label: str, index: str | None, conn) -> None:
        row = measure(conn, engine, index)
        row["insert_100k_seconds"] = measure_insert(args.database)["best_seconds"]
        results[label] = row
        lab.log(
            f"{label[:48]:48} usage {row['currency_count_unfiltered_ms']:>6} / "
            f"{row['currency_count_filtered_ms']:>6} ms  date range {row['date_range_count_ms']:>7} ms  "
            f"insert {row['insert_100k_seconds']} s  views {row['views_ms']}"
        )

    with lab.connect(args.database) as conn:
        record("baseline", None, conn)
        for label, (index, create_sql, table) in CANDIDATES.items():
            started = time.perf_counter()
            conn.execute(create_sql)
            build = time.perf_counter() - started
            conn.execute(f"ANALYZE {table}")
            size = conn.execute(
                f"SELECT pg_relation_size('{index}'::regclass)"
            ).fetchone()[0]
            record(label, index, conn)
            results[label]["build_seconds"] = round(build, 1)
            results[label]["size_mb"] = round(size / 2**20, 1)
            conn.execute(f"DROP INDEX {index}")
            conn.execute(f"ANALYZE {table}")
    lab.save(f"e19_indexes_{args.database}", results)


if __name__ == "__main__":
    main()
