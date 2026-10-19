"""E17: how much the visibility map matters for the directory's counts, what writes do to it,
and what restores it.

Times three count queries (median of 5, with the index-only scan's heap fetches taken from
EXPLAIN ANALYZE), at four moments: as found, after a manual VACUUM, after updating 5% of
employees, and after waiting to see whether autovacuum repairs that, then after VACUUM again.
Writes to the lab database (an updated_at touch on 50,000 employees).

Usage (from backend/):
    python -m scale_lab.e17_vacuum [--database acme_lab_1m] [--wait 180]
"""

import argparse
import re
import statistics
import time

from scale_lab import lab

QUERIES = {
    "count all employees": "SELECT count(*) FROM employees",
    "count active employees": "SELECT count(*) FROM employees WHERE status = 'active'",
    "currency usage (the pay-insight context)": """
        SELECT employees.currency, count(*) FROM employees
        WHERE employees.status = 'active' AND EXISTS (
            SELECT 1 FROM current_compensation
            JOIN compensation_types ON compensation_types.id = current_compensation.compensation_type_id
            WHERE current_compensation.employee_id = employees.id AND compensation_types.counts_toward_total)
        GROUP BY employees.currency""",
}
TABLES = ("employees", "current_compensation")


def table_state(conn) -> dict:
    rows = conn.execute(
        """
        SELECT s.relname, c.relpages, c.relallvisible, s.n_dead_tup, s.n_mod_since_analyze,
               to_char(s.last_autovacuum, 'HH24:MI:SS'), to_char(s.last_vacuum, 'HH24:MI:SS')
        FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid
        WHERE s.relname = ANY(%s)
        """,
        (list(TABLES),),
    ).fetchall()
    return {
        name: {
            "pages": pages,
            "all_visible_percent": round(100 * visible / pages, 1) if pages else None,
            "dead_tuples": dead,
            "modified_since_analyze": modified,
            "last_autovacuum": autovac,
            "last_vacuum": vac,
        }
        for name, pages, visible, dead, modified, autovac, vac in rows
    }


def heap_fetches(conn, sql: str) -> int:
    plan = "\n".join(r[0] for r in conn.execute("EXPLAIN (ANALYZE) " + sql).fetchall())
    return sum(int(n) for n in re.findall(r"Heap Fetches: (\d+)", plan))


def measure(conn, label: str) -> dict:
    out = {"state": table_state(conn), "queries": {}}
    for name, sql in QUERIES.items():
        conn.execute(sql).fetchall()  # warm-up
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            conn.execute(sql).fetchall()
            samples.append((time.perf_counter() - started) * 1000)
        out["queries"][name] = {
            "median_ms": round(statistics.median(samples), 1),
            "heap_fetches": heap_fetches(conn, sql),
        }
    state = out["state"]
    lab.log(
        f"[{label}] all-visible: "
        + ", ".join(f"{t} {state[t]['all_visible_percent']}%" for t in TABLES)
        + " | "
        + " | ".join(
            f"{n.split(' (')[0]}: {q['median_ms']} ms, {q['heap_fetches']:,} fetches"
            for n, q in out["queries"].items()
        )
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_lab_1m")
    parser.add_argument("--wait", type=int, default=180)
    args = parser.parse_args()
    results = {}
    with lab.connect(
        args.database
    ) as conn:  # autocommit: VACUUM cannot run in a transaction
        results["as_found"] = measure(conn, "as found")
        started = time.perf_counter()
        conn.execute("VACUUM (ANALYZE) employees, current_compensation")
        results["vacuum_seconds"] = round(time.perf_counter() - started, 1)
        results["after_vacuum"] = measure(conn, "after VACUUM")
        conn.execute("UPDATE employees SET updated_at = now() WHERE id % 20 = 0")
        results["after_5_percent_updates"] = measure(
            conn, "after updating 5% of employees"
        )
        lab.log(f"waiting {args.wait} s to see whether autovacuum repairs it")
        time.sleep(args.wait)
        results["after_wait"] = measure(conn, f"after waiting {args.wait} s")
        conn.execute("VACUUM (ANALYZE) employees")
        results["after_second_vacuum"] = measure(conn, "after VACUUM again")
    lab.save(f"e17_vacuum_{args.database}", results)


if __name__ == "__main__":
    main()
