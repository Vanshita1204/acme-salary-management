"""E5 — analytics: live aggregation (percentile_cont) vs a materialized view.

Usage (from backend/, after `python -m scale_lab.build --size s|m`):
    python -m scale_lab.e5_analytics --database acme_lab_s

Hypothesis (stated before running): the live views cost a pass over every active
employee's current compensation (E3), so latency grows linearly: fine at 10k, seconds at
1M. A materialized view of one row per active employee (their annual total in USD plus the
dimensions the filters use) turns each view into a scan of one narrow table, several times
faster, and makes filtered views fast with an index. The price is staleness between
refreshes and a refresh that rewrites the whole view, however few employees changed.

Live = the application's own shape: current_compensation joined to the catalog, summed per
employee, converted with the latest stored USD rate, then grouped. The materialized view
stores that per-employee result. Both are compared on the same three views, filtered and
unfiltered, and their answers must agree.

The lab database has no live rates (the provider is not reachable from the lab), so
deterministic stand-in rates are stored, source 'lab', for every currency in use.
"""

import argparse
import time

from scale_lab import lab

MV = "lab_employee_ctc"
SCRATCH_RATES = """
    INSERT INTO exchange_rates (currency, rate_to_usd, rate_date, source)
    SELECT c, 0.002 + (abs(hashtext(c)) % 1000) / 1000.0, current_date, 'lab'
    FROM (SELECT DISTINCT currency AS c FROM employees) used
    ON CONFLICT DO NOTHING
"""
LIVE_BASE = """
    WITH rates AS (
        SELECT DISTINCT ON (currency) currency, rate_to_usd
        FROM exchange_rates ORDER BY currency, rate_date DESC
    ), pop AS (
        SELECT e.id, e.department_id, e.job_title_id, e.job_level_id, e.current_country,
               sum(cc.amount * 12 / ct.period_months) * r.rate_to_usd AS ctc_usd
        FROM employees e
        JOIN current_compensation cc ON cc.employee_id = e.id
        JOIN compensation_types ct ON ct.id = cc.compensation_type_id AND ct.counts_toward_total
        JOIN rates r ON r.currency = e.currency
        WHERE e.status = 'active' {where}
        GROUP BY e.id, r.rate_to_usd
    )
"""
MV_DDL = f"""
    CREATE MATERIALIZED VIEW {MV} AS
    {LIVE_BASE.format(where="")}
    SELECT id AS employee_id, department_id, job_title_id, job_level_id,
           current_country, ctc_usd FROM pop
"""
STATS = """
    SELECT {group}, count(*), round(avg(ctc_usd), 2),
           round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ctc_usd)::numeric, 2),
           round(min(ctc_usd), 2), round(max(ctc_usd), 2)
    FROM {source} GROUP BY 1 ORDER BY 1
"""
SUMMARY = """
    SELECT count(*), round(sum(ctc_usd), 2), round(avg(ctc_usd), 2),
           round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ctc_usd)::numeric, 2)
    FROM {source}
"""
ROLE = """
    SELECT current_country, count(*),
           round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ctc_usd)::numeric, 2)
    FROM {source} WHERE job_title_id = %(title)s GROUP BY 1 ORDER BY 1
"""


def live(view: str, extra_where: str = "", **kw) -> str:
    """The view's query against the live tables."""
    template = {"stats": STATS, "summary": SUMMARY, "role": ROLE}[view]
    body = template.format(group="department_id", source="pop")
    return LIVE_BASE.format(where=extra_where) + body


def stored(view: str) -> str:
    template = {"stats": STATS, "summary": SUMMARY, "role": ROLE}[view]
    return template.format(group="department_id", source=MV)


def workloads(conn) -> list[tuple[str, str, str, dict]]:
    """(name, live SQL, materialized SQL, params)."""
    title = conn.execute(
        "SELECT job_title_id FROM employees GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
    ).fetchone()[0]
    country = conn.execute(
        "SELECT current_country FROM employees GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
    ).fetchone()[0]
    stats_in_country = STATS.format(group="department_id", source=MV).replace(
        f"FROM {MV}", f"FROM {MV} WHERE current_country = '{country}'"
    )
    return [
        ("summary", live("summary"), stored("summary"), {}),
        ("stats by department", live("stats"), stored("stats"), {}),
        (
            f"stats by department, {country} only",
            live("stats", f"AND e.current_country = '{country}'"),
            stats_in_country,
            {},
        ),
        ("one title across countries", live("role"), stored("role"), {"title": title}),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()

    with lab.connect(args.database) as conn:
        conn.execute(SCRATCH_RATES)
        conn.execute(f"DROP MATERIALIZED VIEW IF EXISTS {MV}")
        conn.execute("ANALYZE")
        started = time.perf_counter()
        conn.execute(MV_DDL)
        build = time.perf_counter() - started
        conn.execute(f"CREATE UNIQUE INDEX ON {MV} (employee_id)")
        conn.execute(f"CREATE INDEX ON {MV} (current_country, job_title_id)")
        conn.execute(f"ANALYZE {MV}")
        size = conn.execute(f"SELECT pg_total_relation_size('{MV}')").fetchone()[0]
        rows = conn.execute(f"SELECT count(*) FROM {MV}").fetchone()[0]

        results, disagreements = {}, []
        for name, live_sql, mv_sql, params in workloads(conn):
            a = lab.time_query(conn, live_sql, params)
            b = lab.time_query(conn, mv_sql, params)
            same = (
                conn.execute(live_sql, params).fetchall()
                == conn.execute(mv_sql, params).fetchall()
            )
            if not same:
                disagreements.append(name)
            results[name] = {"live": a, "materialized": b, "same_answer": same}
            lab.log(
                f"E5 {name:<36} live {a['median_ms']:>9} ms | matview {b['median_ms']:>8} ms"
            )

        refresh = {}
        for batch in (100, 1_000, 10_000):
            changed_ids = f"SELECT id FROM employees WHERE status = 'active' ORDER BY id LIMIT {batch}"
            bump = (
                "UPDATE current_compensation SET amount = amount {op} 1 WHERE employee_id IN "
                f"({changed_ids}) AND compensation_type_id = (SELECT id FROM compensation_types WHERE is_base_pay)"
            )
            conn.execute(bump.format(op="+"))
            started = time.perf_counter()
            conn.execute(f"REFRESH MATERIALIZED VIEW {MV}")
            plain = time.perf_counter() - started
            conn.execute(bump.format(op="-"))
            started = time.perf_counter()
            conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {MV}")
            concurrent = time.perf_counter() - started
            refresh[batch] = {
                "plain_seconds": round(plain, 2),
                "concurrent_seconds": round(concurrent, 2),
            }
            lab.log(
                f"E5 refresh after {batch:>6} changes: plain {plain:.2f}s, concurrent {concurrent:.2f}s"
            )
        conn.execute(f"DROP MATERIALIZED VIEW {MV}")
        conn.execute("DELETE FROM exchange_rates WHERE source = 'lab'")

    lab.save(
        f"e5_analytics_{args.database}",
        {
            "database": args.database,
            "view_rows": rows,
            "view_bytes": size,
            "build_seconds": round(build, 2),
            "results": results,
            "refresh_after_changes": refresh,
            "disagreements": disagreements,
        },
    )


if __name__ == "__main__":
    main()
