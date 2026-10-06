"""E7 — index write cost: load the same data with zero, essential and all indexes.

Usage (from backend/):
    python -m scale_lab.e7_index_cost --employees 100000

Hypothesis (stated before running): every secondary index adds work to every insert, so a
bulk load with only the constraint-backing indexes (primary keys, uniques) is clearly
faster than one with all of them, and single-row inserts get slower per index. Reads are
the reverse: without the right index a query scans, and the gain dwarfs the write cost for
this read-heavy HR workload. The "essential" set should capture most of the read benefit
at a fraction of the write cost.

Three fresh databases are loaded with the identical seed (same employees, same records):
- none:      only what constraints require (primary keys, uniques), no other index
- essential: none + the history lookup (employee, type, date), the partial future-dated
             index, and the name sort index; the three the app can't do without
- all:       the schema's full set (what migrations create)

For each: bulk-load seconds, then read latency on six representative queries, then the
latency of single-row inserts (compensation records and employees, autocommit). The
databases are throwaway, so the append-only rows written for the insert test are simply
dropped with them.
"""

import argparse
import random
import statistics
import time

from scale_lab import lab

ESSENTIAL = {
    "ix_comp_records_employee_type_effdate",
    "ix_comp_records_future_dated",
    "ix_employees_name_search",
}
TABLES = ("employees", "compensation_records", "current_compensation")
SECONDARY = """
    SELECT i.indexname FROM pg_indexes i
    WHERE i.schemaname = 'public' AND i.tablename = ANY(%(tables)s)
      AND i.indexname NOT IN (SELECT conname FROM pg_constraint WHERE contype IN ('p', 'u'))
"""

READS = {
    "department + country, by name": (
        "SELECT id FROM employees WHERE department_id = %(dept)s AND current_country = %(country)s "
        "ORDER BY lower(last_name), lower(first_name), id LIMIT 50"
    ),
    "name page (keyset, deep)": (
        "SELECT id FROM employees WHERE (lower(last_name), lower(first_name), id) > ('m', '', 0) "
        "ORDER BY lower(last_name), lower(first_name), id LIMIT 50"
    ),
    "hire date page": (
        "SELECT id FROM employees WHERE (hire_date, id) > ('2022-01-01', 0) "
        "ORDER BY hire_date, id LIMIT 50"
    ),
    "one employee's history": (
        "SELECT * FROM compensation_records WHERE employee_id = %(emp)s "
        "ORDER BY effective_date DESC, id DESC"
    ),
    "level filter count": "SELECT count(*) FROM employees WHERE job_level_id = %(level)s AND status = 'active'",
    "future-dated records": (
        "SELECT employee_id FROM compensation_records "
        "WHERE effective_date > (created_at AT TIME ZONE 'UTC')::date AND effective_date <= current_date + 30"
    ),
}


def drop_secondary(database: str, keep: set[str]) -> list[str]:
    with lab.connect(database) as conn:
        names = [
            r[0] for r in conn.execute(SECONDARY, {"tables": list(TABLES)}).fetchall()
        ]
        for name in names:
            if name not in keep:
                conn.execute(f'DROP INDEX "{name}"')
        return names


def index_bytes(database: str) -> int:
    with lab.connect(database) as conn:
        return conn.execute(
            "SELECT sum(pg_indexes_size(c.oid))::bigint FROM pg_class c WHERE c.relname = ANY(%(t)s)",
            {"t": list(TABLES)},
        ).fetchone()[0]


def build(config: str, employees: int) -> dict:
    database = f"acme_lab_e7_{config}"
    lab.prepare_reference(database)
    if config != "all":
        drop_secondary(database, ESSENTIAL if config == "essential" else set())
    seconds = lab.seed(database, employees, "copy")
    lab.analyze(database)
    with lab.connect(database) as conn:
        remaining = [
            r[0] for r in conn.execute(SECONDARY, {"tables": list(TABLES)}).fetchall()
        ]
    lab.log(
        f"E7 {config:<9} loaded {employees:,} employees in {seconds:.1f}s with {len(remaining)} secondary indexes"
    )
    return {
        "database": database,
        "load_seconds": round(seconds, 1),
        "secondary_indexes": len(remaining),
        "index_bytes": index_bytes(database),
        "indexes": sorted(remaining),
    }


def read_latency(database: str) -> dict:
    with lab.connect(database) as conn:
        params = {
            "dept": conn.execute(
                "SELECT department_id FROM employees GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
            ).fetchone()[0],
            "country": "US",
            "emp": conn.execute(
                "SELECT id FROM employees ORDER BY id OFFSET 100 LIMIT 1"
            ).fetchone()[0],
            "level": conn.execute(
                "SELECT job_level_id FROM employees GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
            ).fetchone()[0],
        }
        out = {}
        for name, sql in READS.items():
            out[name] = lab.time_query(conn, sql, params)["median_ms"]
        return out


def insert_latency(database: str, count: int = 2000) -> dict:
    """Single-row autocommit inserts: new bonus records for existing employees (the
    common write), then new employees."""
    rng = random.Random(7)
    with lab.connect(database) as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM employees ORDER BY id LIMIT 20000"
            ).fetchall()
        ]
        row = conn.execute(
            "SELECT (SELECT id FROM compensation_types WHERE category = 'bonus' AND subtype = 'annual'),"
            " (SELECT id FROM change_reasons WHERE code = 'annual_revision')"
        ).fetchone()
        bonus_type, reason = row
        record_sql = (
            "INSERT INTO compensation_records (employee_id, compensation_type_id, change_reason_id, effective_date, "
            "country, currency, amount, changed_by) SELECT id, %s, %s, current_date, current_country, currency, 1000, 'lab' "
            "FROM employees WHERE id = %s"
        )
        samples = []
        for _ in range(count):
            started = time.perf_counter()
            conn.execute(record_sql, (bonus_type, reason, rng.choice(ids)))
            samples.append((time.perf_counter() - started) * 1000)
        records = statistics.median(samples)
        template = conn.execute(
            "SELECT company_id, department_id, job_title_id, job_level_id FROM employees LIMIT 1"
        ).fetchone()
        samples = []
        for n in range(count // 4):
            started = time.perf_counter()
            conn.execute(
                "INSERT INTO employees (company_id, first_name, last_name, email, department_id, job_title_id, "
                "job_level_id, current_country, currency, status, hire_date) VALUES "
                "(%s, 'Lab', 'Insert', %s, %s, %s, %s, 'US', 'USD', 'active', '2024-01-01')",
                (
                    template[0],
                    f"e7.{n}@lab.example",
                    template[1],
                    template[2],
                    template[3],
                ),
            )
            samples.append((time.perf_counter() - started) * 1000)
        employees = statistics.median(samples)
    return {
        "record_insert_median_ms": round(records, 3),
        "employee_insert_median_ms": round(employees, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--employees", type=int, default=100_000)
    parser.add_argument("--configs", nargs="*", default=["none", "essential", "all"])
    args = parser.parse_args()
    results = {}
    for config in args.configs:
        entry = build(config, args.employees)
        entry["reads_ms"] = read_latency(entry["database"])
        entry["inserts"] = insert_latency(entry["database"])
        lab.log(f"E7 {config:<9} reads {entry['reads_ms']} inserts {entry['inserts']}")
        results[config] = entry
    lab.save(
        f"e7_index_cost_{args.employees}",
        {"employees": args.employees, "results": results},
    )


if __name__ == "__main__":
    main()
