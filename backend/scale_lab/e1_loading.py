"""E1 — bulk loading: row-by-row INSERT vs ORM unit of work vs batched INSERT vs COPY.

Usage (from backend/):
    python -m scale_lab.e1_loading --count 10000 [--methods row orm batch copy]

Each method seeds the same employees into a fresh lab database; a checksum over the
loaded data proves every method wrote identical rows, so only speed differs.
"""

import argparse

from scale_lab import lab

DATABASE = "acme_lab_e1"

# Order-independent fingerprint of the generated data (ids and timestamps excluded,
# since those legitimately differ between loads): the sum of a 60-bit hash per record.
# Unlike string_agg, it doesn't hit Postgres's 1 GB string limit at millions of rows.
CHECKSUM = """
SELECT sum(('x' || substr(md5(concat_ws(',', e.email, e.hire_date, e.status, e.currency,
            ct.subtype, r.effective_date, r.amount, cr.code)), 1, 15))::bit(60)::bigint::numeric)
FROM compensation_records r
JOIN employees e ON e.id = r.employee_id
JOIN compensation_types ct ON ct.id = r.compensation_type_id
JOIN change_reasons cr ON cr.id = r.change_reason_id
"""


def run(count: int, methods: list[str]) -> dict:
    results = {}
    for method in methods:
        lab.prepare_reference(DATABASE)
        seconds = lab.seed(DATABASE, count, method)
        with lab.connect(DATABASE) as conn:
            sizes = lab.table_sizes(conn)
            records = conn.execute(
                "SELECT count(*) FROM compensation_records"
            ).fetchone()[0]
            checksum = conn.execute(CHECKSUM).fetchone()[0]
        rows = count + records
        results[method] = {
            "seconds": round(seconds, 1),
            "employees": count,
            "records": records,
            "rows_per_second": round(rows / seconds),
            "checksum": checksum,
            "sizes": sizes,
        }
        lab.log(
            f"E1 {method:>5} @ {count:,}: {seconds:.1f}s, {rows / seconds:,.0f} rows/s"
        )
    checksums = {r["checksum"] for r in results.values()}
    lab.log(f"identical data across methods: {len(checksums) == 1}")
    return {"count": count, "identical_data": len(checksums) == 1, "methods": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10_000)
    parser.add_argument("--methods", nargs="+", default=["row", "orm", "batch", "copy"])
    args = parser.parse_args()
    payload = run(args.count, args.methods)
    lab.save(f"e1_loading_{args.count}", payload)
    lab.admin_execute(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')


if __name__ == "__main__":
    main()
