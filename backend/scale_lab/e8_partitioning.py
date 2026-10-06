"""E8 — partitioning: range-partition compensation records by effective date.

Usage (from backend/, after `python -m scale_lab.build --size m`):
    python -m scale_lab.e8_partitioning --database acme_lab_m

The plan calls for the L dataset (10 million employees, ~30 million+ records). L needs
well over 100 GB of disk and many hours to load on this machine, so this runs on M (1
million employees, ~18 million records) instead; the result is recorded as M's, and the
direction of each effect is what carries over, not the absolute numbers.

Hypothesis (stated before running): a date-range report (records effective in one
quarter) scans every record without partitioning and only the matching partition with it,
so it should be several times faster, and no worse than a single-column date index. The
cost shows up elsewhere: lookups that don't filter by date (one employee's history, one
record by id) have to probe every partition, and the schema loses the ability to keep
`id` unique on its own, which the current-compensation pointer's foreign key relies on.

Three identical copies of the table are compared, none with triggers or foreign keys so
only the physical layout differs:
- plain:            one table, the composite history index
- plain + date idx: the same plus an index on effective_date alone
- partitioned:      yearly partitions, the same composite index on each
"""

import argparse
import statistics
import time

from scale_lab import lab

COLUMNS = (
    "id bigint NOT NULL, employee_id bigint NOT NULL, compensation_type_id bigint NOT NULL, "
    "effective_date date NOT NULL, country char(2) NOT NULL, currency char(3) NOT NULL, "
    "amount numeric(14,2) NOT NULL, change_reason_id bigint NOT NULL, note text, "
    "changed_by text NOT NULL, created_at timestamptz NOT NULL"
)
NAMES = (
    "id, employee_id, compensation_type_id, effective_date, country, currency, amount, "
    "change_reason_id, note, changed_by, created_at"
)
HISTORY_INDEX = "(employee_id, compensation_type_id, effective_date DESC)"
FIRST_REPORT_YEAR = 2025


def setup(conn, years: range) -> dict:
    """Create and fill the three copies; return build seconds for each."""
    for table in ("lab_plain", "lab_part"):
        conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    times = {}
    conn.execute(f"CREATE TABLE lab_plain ({COLUMNS}, PRIMARY KEY (id))")
    conn.execute(
        f"CREATE TABLE lab_part ({COLUMNS}, PRIMARY KEY (id, effective_date)) "
        "PARTITION BY RANGE (effective_date)"
    )
    for year in years:
        conn.execute(
            f"CREATE TABLE lab_part_{year} PARTITION OF lab_part "
            f"FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')"
        )
    conn.execute("CREATE TABLE lab_part_default PARTITION OF lab_part DEFAULT")
    for table in ("lab_plain", "lab_part"):
        started = time.perf_counter()
        conn.execute(f"INSERT INTO {table} SELECT {NAMES} FROM compensation_records")
        times[f"{table}_load"] = round(time.perf_counter() - started, 1)
        started = time.perf_counter()
        conn.execute(f"CREATE INDEX ON {table} {HISTORY_INDEX}")
        times[f"{table}_history_index"] = round(time.perf_counter() - started, 1)
        lab.log(
            f"E8 {table}: {times[f'{table}_load']}s load, {times[f'{table}_history_index']}s index"
        )
    started = time.perf_counter()
    conn.execute("CREATE INDEX lab_plain_date ON lab_plain (effective_date)")
    times["lab_plain_date_index"] = round(time.perf_counter() - started, 1)
    conn.execute("ANALYZE lab_plain")
    conn.execute("ANALYZE lab_part")
    return times


def structural_limits(conn) -> dict:
    """What the schema can no longer say. Each statement is attempted, the error recorded."""
    attempts = {
        "primary key on id alone": (
            "CREATE TABLE lab_bad (id bigint, effective_date date, PRIMARY KEY (id)) "
            "PARTITION BY RANGE (effective_date)"
        ),
        "unique id on the partitioned table": "ALTER TABLE lab_part ADD CONSTRAINT lab_u UNIQUE (id)",
    }
    out = {}
    for name, sql in attempts.items():
        try:
            conn.execute(sql)
            out[name] = "allowed"
            conn.execute("DROP TABLE IF EXISTS lab_bad")
        except Exception as error:  # noqa: BLE001  the message is the finding
            out[name] = str(error).splitlines()[0]
    return out


def workloads(conn, first: int) -> dict[str, str]:
    employees = conn.execute("SELECT count(*) FROM employees").fetchone()[0]
    records = conn.execute("SELECT count(*) FROM lab_plain").fetchone()[0]
    emp = conn.execute(
        "SELECT id FROM employees ORDER BY id OFFSET %s LIMIT 1", (employees * 2 // 5,)
    ).fetchone()[0]
    rec = conn.execute(
        "SELECT id FROM lab_plain ORDER BY id OFFSET %s LIMIT 1", (records // 2,)
    ).fetchone()[0]
    quarter = f"effective_date >= '{first}-01-01' AND effective_date < '{first}-04-01'"
    year = f"effective_date >= '{first}-01-01' AND effective_date < '{first + 1}-01-01'"
    ids = f"SELECT id FROM employees WHERE id >= {emp} ORDER BY id LIMIT 50"
    return {
        "records in one quarter (count, sum)": f"SELECT count(*), sum(amount) FROM {{t}} WHERE {quarter}",
        "one year by reason": (
            f"SELECT change_reason_id, count(*), avg(amount) FROM {{t}} WHERE {year} GROUP BY 1 ORDER BY 1"
        ),
        "one employee's history": f"SELECT * FROM {{t}} WHERE employee_id = {emp} ORDER BY effective_date DESC",
        "one record by id": f"SELECT * FROM {{t}} WHERE id = {rec}",
        "current per type, 50 employees (DISTINCT ON)": (
            f"SELECT DISTINCT ON (r.employee_id, r.compensation_type_id) r.employee_id, r.amount FROM {{t}} r "
            f"WHERE r.employee_id IN ({ids}) AND r.effective_date <= '{first}-10-01' "
            "ORDER BY r.employee_id, r.compensation_type_id, r.effective_date DESC, r.id DESC"
        ),
    }


def insert_latency(conn, table: str, count: int = 2000) -> float:
    base = conn.execute(
        "SELECT coalesce(max(id), 0) + 1000000000 FROM lab_plain"
    ).fetchone()[0]
    samples = []
    for n in range(count):
        started = time.perf_counter()
        conn.execute(
            f"INSERT INTO {table} ({NAMES}) VALUES (%s, 1, 1, '2026-09-30', 'US', 'USD', 1, 1, NULL, 'lab', now())",
            (base + n,),
        )
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 3)


def pruning(plan: list[str]) -> str:
    """How many partitions the plan touched (from the plan text)."""
    scanned = sum(1 for line in plan if "lab_part_20" in line and ("Scan" in line))
    return f"{scanned} yearly partitions scanned" if scanned else ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()

    with lab.connect(args.database) as conn:
        low, high = conn.execute(
            "SELECT extract(year FROM min(effective_date)), extract(year FROM max(effective_date)) "
            "FROM compensation_records"
        ).fetchone()
        years = range(int(low), int(high) + 1)
        records = conn.execute("SELECT count(*) FROM compensation_records").fetchone()[
            0
        ]
        build = setup(conn, years)
        first = min(max(FIRST_REPORT_YEAR, int(low)), int(high))
        results = {}
        for name, template in workloads(conn, first).items():
            row = {}
            for label, table in (
                ("plain", "lab_plain"),
                ("plain_date_index", "lab_plain"),
                ("partitioned", "lab_part"),
            ):
                if label == "plain":
                    conn.execute("DROP INDEX IF EXISTS lab_plain_date")
                    conn.execute("ANALYZE lab_plain")
                elif label == "plain_date_index":
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS lab_plain_date ON lab_plain (effective_date)"
                    )
                    conn.execute("ANALYZE lab_plain")
                row[label] = lab.time_query(conn, template.format(t=table))
                row[label]["pruning"] = pruning(row[label]["plan"])
            results[name] = row
            lab.log(
                f"E8 {name:<46} plain {row['plain']['median_ms']:>9} | +date idx {row['plain_date_index']['median_ms']:>9}"
                f" | partitioned {row['partitioned']['median_ms']:>9} ms"
            )
        conn.execute("DROP INDEX IF EXISTS lab_plain_date")
        limits = structural_limits(conn)
        inserts = {
            "plain": insert_latency(conn, "lab_plain"),
            "partitioned": insert_latency(conn, "lab_part"),
        }
        sizes = {
            "lab_plain": conn.execute(
                "SELECT pg_total_relation_size('lab_plain')"
            ).fetchone()[0],
            # The parent holds no rows: add up every partition (and the parent's own indexes).
            "lab_part": conn.execute(
                "SELECT pg_total_relation_size('lab_part') + coalesce(sum(pg_total_relation_size(inhrelid)), 0)::bigint "
                "FROM pg_inherits WHERE inhparent = 'lab_part'::regclass"
            ).fetchone()[0],
        }
        for table in ("lab_plain", "lab_part"):
            conn.execute(f"DROP TABLE {table} CASCADE")

    lab.save(
        f"e8_partitioning_{args.database}",
        {
            "database": args.database,
            "records": records,
            "partitions": len(years) + 1,
            "build": build,
            "results": results,
            "single_insert_median_ms": inserts,
            "total_bytes": sizes,
            "structural_limits": limits,
            "note": "Run on M, not L: see module docstring.",
        },
    )


if __name__ == "__main__":
    main()
