"""E2 — pagination: OFFSET vs keyset, on the first, middle and last directory page.

Usage (from backend/, after `python -m scale_lab.build --size s|m`):
    python -m scale_lab.e2_pagination --database acme_lab_s

Two directory sorts are measured: by name (the FR-1 default) and by hire date. The
name sort is run twice for keyset — with the schema's existing index
`(lower(last_name), lower(first_name))`, and with a variant that also includes `id`,
the keyset tie-breaker — to see whether the tie-breaker needs to be in the index.
"""

import argparse

from scale_lab import lab

PAGE = 50
NAME_ORDER = "lower(last_name), lower(first_name), id"
HIRE_ORDER = "hire_date, id"
COLUMNS = "id, code, first_name, last_name, department, job_title, job_level, current_country, status, hire_date"
NAME_WITH_ID_INDEX = "lab_ix_employees_name_id"


def offset_sql(order: str) -> str:
    return f"SELECT {COLUMNS} FROM employees ORDER BY {order} LIMIT {PAGE} OFFSET %(offset)s"


def keyset_sql(order: str) -> str:
    """Row-value comparison on the sort key, one bound parameter per key column."""
    width = len(order.split(", "))
    placeholders = ", ".join(f"%(k{i})s" for i in range(width))
    return (
        f"SELECT {COLUMNS} FROM employees WHERE ({order}) > ({placeholders}) "
        f"ORDER BY {order} LIMIT {PAGE}"
    )


def cursor_at(conn, order: str, offset: int) -> dict | None:
    """The sort key of the row just before `offset` — what a client would hold."""
    if offset == 0:
        return None
    row = conn.execute(
        f"SELECT {order} FROM employees ORDER BY {order} LIMIT 1 OFFSET %(o)s",
        {"o": offset - 1},
    ).fetchone()
    return {f"k{i}": value for i, value in enumerate(row)}


def measure(conn, label: str, order: str, total: int) -> dict:
    positions = {"first": 0, "middle": total // 2, "last": total - PAGE}
    out = {}
    for position, offset in positions.items():
        out[f"offset/{position}"] = lab.time_query(
            conn, offset_sql(order), {"offset": offset}
        )
        cursor = cursor_at(conn, order, offset)
        if cursor is None:  # first page: no cursor, plain ORDER BY ... LIMIT
            sql, params = offset_sql(order), {"offset": 0}
        else:
            sql, params = keyset_sql(order), cursor
        out[f"keyset/{position}"] = lab.time_query(conn, sql, params)
        lab.log(
            f"E2 {label:<16} {position:<6} offset {out[f'offset/{position}']['median_ms']:>8} ms"
            f" | keyset {out[f'keyset/{position}']['median_ms']:>8} ms"
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()

    with lab.connect(args.database) as conn:
        total = conn.execute("SELECT count(*) FROM employees").fetchone()[0]
        results = {
            "hire_date": measure(conn, "hire_date", HIRE_ORDER, total),
            "name": measure(conn, "name", NAME_ORDER, total),
        }
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {NAME_WITH_ID_INDEX} "
            f"ON employees (lower(last_name), lower(first_name), id)"
        )
        conn.execute("ANALYZE employees")
        try:
            results["name+id_index"] = measure(
                conn, "name (+id index)", NAME_ORDER, total
            )
        finally:
            conn.execute(f"DROP INDEX IF EXISTS {NAME_WITH_ID_INDEX}")

    lab.save(
        f"e2_pagination_{args.database}",
        {
            "database": args.database,
            "employees": total,
            "page_size": PAGE,
            "results": results,
        },
    )


if __name__ == "__main__":
    main()
