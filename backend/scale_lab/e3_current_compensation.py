"""E3 — current compensation: DISTINCT ON vs ROW_NUMBER() vs the current_compensation table.

Usage (from backend/, after `python -m scale_lab.build --size s|m`):
    python -m scale_lab.e3_current_compensation --database acme_lab_s

"Current compensation" = each employee's latest in-effect record per compensation type.
Three workloads, each answered three ways:
- page:      a 50-employee directory page (by name) with each employee's total (CTC)
- aggregate: total CTC per country across all active employees
- top:       the 50 highest-paid employees by total (directory sorted by compensation)
The two on-the-fly strategies run with and without the composite index
`ix_comp_records_employee_type_effdate`; the table strategy doesn't use it.
"""

import argparse

from scale_lab import lab

AS_OF = lab.AS_OF
INDEX = "ix_comp_records_employee_type_effdate"
INDEX_DDL = (
    f"CREATE INDEX {INDEX} ON compensation_records "
    "(employee_id, compensation_type_id, effective_date DESC)"
)

# Latest in-effect record per (employee, type), computed from history. {scope} narrows
# the employees considered (a page) or is empty (everyone).
DISTINCT_ON = """
    SELECT DISTINCT ON (r.employee_id, r.compensation_type_id)
           r.employee_id, r.compensation_type_id, r.amount
    FROM compensation_records r
    WHERE r.effective_date <= %(as_of)s {scope}
    ORDER BY r.employee_id, r.compensation_type_id, r.effective_date DESC, r.id DESC
"""
ROW_NUMBER = """
    SELECT employee_id, compensation_type_id, amount FROM (
        SELECT r.employee_id, r.compensation_type_id, r.amount,
               row_number() OVER (PARTITION BY r.employee_id, r.compensation_type_id
                                  ORDER BY r.effective_date DESC, r.id DESC) AS rn
        FROM compensation_records r
        WHERE r.effective_date <= %(as_of)s {scope}
    ) ranked WHERE rn = 1
"""
TABLE = """
    SELECT cc.employee_id, cc.compensation_type_id, cc.amount
    FROM current_compensation cc
    WHERE true {scope}
"""
STRATEGIES = {"distinct_on": DISTINCT_ON, "row_number": ROW_NUMBER, "table": TABLE}

ANNUAL_CTC = (
    "sum(c.amount * 12 / ct.period_months) FILTER (WHERE ct.counts_toward_total)"
)
PAGE_IDS = (
    "SELECT id FROM employees ORDER BY lower(last_name), lower(first_name), id LIMIT 50"
)


def page_sql(current: str) -> str:
    alias = "cc" if current is TABLE else "r"
    current = current.format(scope=f"AND {alias}.employee_id IN (SELECT id FROM page)")
    return f"""
        WITH page AS ({PAGE_IDS}), current AS ({current})
        SELECT e.id, e.first_name, e.last_name, e.currency, {ANNUAL_CTC} AS total
        FROM employees e
        JOIN page p ON p.id = e.id
        JOIN current c ON c.employee_id = e.id
        JOIN compensation_types ct ON ct.id = c.compensation_type_id
        GROUP BY e.id
        ORDER BY lower(e.last_name), lower(e.first_name), e.id
    """


def aggregate_sql(current: str) -> str:
    return f"""
        WITH current AS ({current.format(scope="")})
        SELECT e.current_country, e.currency, count(DISTINCT e.id) AS employees,
               {ANNUAL_CTC} AS total_ctc
        FROM employees e
        JOIN current c ON c.employee_id = e.id
        JOIN compensation_types ct ON ct.id = c.compensation_type_id
        WHERE e.status = 'active'
        GROUP BY e.current_country, e.currency
    """


def top_sql(current: str) -> str:
    return f"""
        WITH current AS ({current.format(scope="")})
        SELECT c.employee_id, {ANNUAL_CTC} AS total
        FROM current c
        JOIN compensation_types ct ON ct.id = c.compensation_type_id
        GROUP BY c.employee_id
        ORDER BY total DESC NULLS LAST, c.employee_id
        LIMIT 50
    """


WORKLOADS = {"page": page_sql, "aggregate": aggregate_sql, "top": top_sql}


def run_all(conn, index_state: str, strategies: list[str]) -> dict:
    out = {}
    for workload, build in WORKLOADS.items():
        for strategy in strategies:
            result = lab.time_query(conn, build(STRATEGIES[strategy]), {"as_of": AS_OF})
            out[f"{workload}/{strategy}"] = result
            lab.log(
                f"E3 [{index_state:<8}] {workload:<9} {strategy:<11} {result['median_ms']:>10} ms"
            )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()

    with lab.connect(args.database) as conn:
        results = {"with_index": run_all(conn, "index", list(STRATEGIES))}
        conn.execute(f"DROP INDEX {INDEX}")
        conn.execute("ANALYZE compensation_records")
        try:
            results["without_index"] = run_all(
                conn, "no index", ["distinct_on", "row_number"]
            )
        finally:
            conn.execute(INDEX_DDL)
            conn.execute("ANALYZE compensation_records")
        sizes = lab.table_sizes(conn)

    lab.save(
        f"e3_current_compensation_{args.database}",
        {"database": args.database, "as_of": AS_OF, "sizes": sizes, "results": results},
    )


if __name__ == "__main__":
    main()
