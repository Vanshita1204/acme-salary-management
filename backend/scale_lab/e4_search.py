"""E4 — search: ILIKE '%term%' with no index vs a B-tree prefix index vs pg_trgm GIN.

Usage (from backend/, after `python -m scale_lab.build --size s|m`):
    python -m scale_lab.e4_search --database acme_lab_s

Hypothesis (stated before running): the directory's search is a substring match over four
expressions (full name, last name, email, code). A B-tree can only serve a *prefix* of one
column, so it can't answer the same question; a trigram GIN index per expression can, for
terms of 3+ characters. Terms of 1-2 characters have no trigram to look up, so they should
stay a full scan. At 10k nothing matters; at 1M the scan should cost hundreds of ms and the
GIN index should cut selective terms to milliseconds, at the price of index size and build
time, and slower writes (measured in E7).

Planning matters here as much as the index: the timed runs use a connection that never
prepares statements (every run gets a plan made for its own search term). A last arm uses
the driver's default, which prepares a statement after 5 executions and lets Postgres
switch to one generic plan that can't know the term is rare; it is measured after that
switch. That arm is what the application's own connections do.

Every strategy runs the directory's real page query (search, name order, 51 rows). The
B-tree strategy answers a different question (last-name prefix only), so it is compared on
last-name-prefix terms and its result is not expected to equal the substring result.
"""

import argparse
import statistics
import time

import psycopg

from scale_lab import lab

PAGE = "ORDER BY lower(last_name), lower(first_name), id LIMIT 51"
COLUMNS = "id, code, first_name, last_name, email, current_country, status"
FULL_NAME = "(first_name || ' ' || last_name)"
SEARCH_EXPRESSIONS = [FULL_NAME, "last_name", "(email::text)", "code"]
SEARCH = "(" + " OR ".join(f"{e} ILIKE %(p)s" for e in SEARCH_EXPRESSIONS) + ")"

# (label, term): short terms have no trigram; selective ones match few rows.
TERMS = [
    ("2 chars", "an"),
    ("3 chars, common", "son"),
    ("surname", "abbott"),
    ("full name part", "joan abb"),
    ("code part", "emp-0065"),
    ("email part", "abbott.659"),
    ("no match", "zzzqqq"),
]
PREFIX_TERMS = [("prefix 'ab'", "ab"), ("prefix 'abbott'", "abbott")]

TRIGRAM_INDEXES = {
    "lab_trgm_fullname": f"{FULL_NAME} gin_trgm_ops",
    "lab_trgm_last": "last_name gin_trgm_ops",
    "lab_trgm_email": "(email::text) gin_trgm_ops",
    "lab_trgm_code": "code gin_trgm_ops",
}
PREFIX_INDEX = "lab_btree_last_prefix"


def like(term: str) -> str:
    return (
        "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )


def search_sql() -> str:
    return f"SELECT {COLUMNS} FROM employees WHERE {SEARCH} {PAGE}"


def count_sql() -> str:
    return f"SELECT count(*) FROM employees WHERE {SEARCH}"


def prefix_sql() -> str:
    return f"SELECT {COLUMNS} FROM employees WHERE lower(last_name) LIKE %(p)s {PAGE}"


def run_terms(conn, label: str, terms, sql: str, count: str | None) -> dict:
    out = {}
    for name, term in terms:
        params = {"p": like(term) if "ILIKE" in sql else term.lower() + "%"}
        result = lab.time_query(conn, sql, params)
        if count:
            result["matches"] = conn.execute(count, params).fetchone()[0]
        out[name] = result
        lab.log(
            f"E4 {label:<10} {name:<18} {result['median_ms']:>9} ms"
            + (f"  ({result['matches']:,} matches)" if count else "")
        )
    return out


def steady_state(
    conn, sql: str, params: dict, warmup: int = 12, runs: int = 5
) -> float:
    """Median latency once the driver has prepared the statement and Postgres has had the
    chance to settle on a generic plan (5 unprepared + 5 custom-plan executions)."""
    for _ in range(warmup):
        conn.execute(sql, params).fetchall()
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        conn.execute(sql, params).fetchall()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 2)


def drop_all(conn) -> None:
    for name in [*TRIGRAM_INDEXES, PREFIX_INDEX]:
        conn.execute(f"DROP INDEX IF EXISTS {name}")


def index_sizes(conn, names) -> dict:
    return {
        name: conn.execute("SELECT pg_relation_size(%s::regclass)", (name,)).fetchone()[
            0
        ]
        for name in names
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()

    unprepared = psycopg.connect(
        lab.libpq_url(args.database), autocommit=True, prepare_threshold=None
    )
    with unprepared as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        total = conn.execute("SELECT count(*) FROM employees").fetchone()[0]
        drop_all(conn)
        conn.execute("ANALYZE employees")
        results = {
            "no_index": run_terms(conn, "no index", TERMS, search_sql(), count_sql())
        }
        results["no_index_prefix_query"] = run_terms(
            conn, "no idx pfx", PREFIX_TERMS, prefix_sql(), None
        )

        started = time.perf_counter()
        conn.execute(
            f"CREATE INDEX {PREFIX_INDEX} ON employees (lower(last_name) text_pattern_ops)"
        )
        prefix_build = time.perf_counter() - started
        conn.execute("ANALYZE employees")
        results["btree_prefix"] = run_terms(
            conn, "btree pfx", PREFIX_TERMS, prefix_sql(), None
        )
        prefix_size = index_sizes(conn, [PREFIX_INDEX])[PREFIX_INDEX]
        conn.execute(f"DROP INDEX {PREFIX_INDEX}")

        build = {}
        for name, definition in TRIGRAM_INDEXES.items():
            started = time.perf_counter()
            conn.execute(f"CREATE INDEX {name} ON employees USING gin ({definition})")
            build[name] = round(time.perf_counter() - started, 2)
        conn.execute("ANALYZE employees")
        results["trigram_gin"] = run_terms(
            conn, "trigram", TERMS, search_sql(), count_sql()
        )
        with lab.connect(args.database) as prepared:  # the driver's default behaviour
            results["trigram_gin_driver_default"] = {
                name: {
                    "median_ms": steady_state(prepared, search_sql(), {"p": like(term)})
                }
                for name, term in TERMS
            }
        for name, row in results["trigram_gin_driver_default"].items():
            lab.log(f"E4 trigram+driver default {name:<18} {row['median_ms']:>9} ms")
        gin_sizes = index_sizes(conn, TRIGRAM_INDEXES)
        # Same query, same answers? The index must change speed, never results.
        mismatches = [
            name
            for name, _ in TERMS
            if results["no_index"][name]["matches"]
            != results["trigram_gin"][name]["matches"]
        ]
        drop_all(conn)
        conn.execute("ANALYZE employees")

    lab.save(
        f"e4_search_{args.database}",
        {
            "database": args.database,
            "employees": total,
            "results": results,
            "btree_prefix_index": {
                "build_seconds": round(prefix_build, 2),
                "bytes": prefix_size,
            },
            "trigram_indexes": {
                "build_seconds": build,
                "bytes": gin_sizes,
                "total_build_seconds": round(sum(build.values()), 2),
                "total_bytes": sum(gin_sizes.values()),
            },
            "result_mismatches": mismatches,
        },
    )


if __name__ == "__main__":
    main()
