"""E15: rank the SQL statements by total database time over a realistic mix of requests.

Runs the real server against a lab database (read-only requests: no pay changes or
imports) with a few clients for a fixed time, with `pg_stat_statements` recording every
normalised statement, then prints and saves the top ones.

Usage (from backend/):
    python -m scale_lab.e15_statements [--database acme_lab_1m] [--seconds 90] [--clients 4]

The database must have the extension loaded (docker-compose.yml preloads it) and created
(`CREATE EXTENSION pg_stat_statements` in the lab database).
"""

import argparse
import re

import httpx

from scale_lab import lab
from scale_lab.e9_concurrency import BASE, drive, resolve, server

REFERENCE = [
    (path.strip("/"), path, {})
    for path in (
        "/departments",
        "/job-titles",
        "/job-levels",
        "/countries",
        "/currencies",
        "/companies",
        "/compensation-types",
        "/change-reasons",
        "/exchange-rates/latest",
    )
]
DIRECTORY = [
    ("directory first page", "/employees", {"limit": 50}),
    ("directory search", "/employees", {"q": "abbott", "limit": 50}),
    (
        "directory filtered",
        "/employees",
        {"department": "Engineering", "country": "IN", "limit": 50},
    ),
]
PROFILES = [(f"profile {n}", f"/employees/{n}", {}) for n in (1_000, 50_000, 500_000)]
INSIGHTS = [
    ("summary", "/analytics/summary", {}),
    ("stats", "/analytics/stats", {"group_by": "department"}),
    ("role by country", "/analytics/role-by-country", {}),
    ("histogram", "/analytics/histogram", {"bins": 20}),
    ("outliers", "/analytics/outliers", {}),
    ("composition", "/analytics/composition", {}),
    (
        "pay changes",
        "/analytics/changes",
        {"date_from": "2026-09-17", "date_to": "2026-10-17", "limit": 25},
    ),
]
EXPORT = [("export", "/employees/export", {"q": "abbott"})]
SESSION = REFERENCE + DIRECTORY + PROFILES + INSIGHTS + EXPORT


def one_line(sql: str, width: int = 120) -> str:
    return re.sub(r"\s+", " ", sql).strip()[:width]


def top_statements(database: str, limit: int = 15) -> dict:
    with lab.connect(database) as conn:
        total = conn.execute(
            "SELECT sum(total_exec_time) FROM pg_stat_statements WHERE dbid = "
            "(SELECT oid FROM pg_database WHERE datname = current_database())"
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT query, calls, total_exec_time, mean_exec_time, max_exec_time, rows
            FROM pg_stat_statements
            WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
              AND query NOT ILIKE '%%pg_stat_statements%%'
            ORDER BY total_exec_time DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return {
        "total_ms": round(total, 1),
        "statements": [
            {
                "query": one_line(q, 400),
                "calls": calls,
                "total_ms": round(t, 1),
                "share_percent": round(100 * t / total, 1),
                "mean_ms": round(mean, 2),
                "max_ms": round(mx, 1),
                "rows": r,
            }
            for q, calls, t, mean, mx, r in rows
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_lab_1m")
    parser.add_argument("--seconds", type=float, default=90)
    parser.add_argument("--clients", type=int, default=4)
    args = parser.parse_args()

    with (
        server(args.database, 1, 5, 10),
        httpx.Client(base_url=BASE, timeout=120) as client,
    ):
        requests = [
            (lbl, path, resolve(client, params)) for lbl, path, params in SESSION
        ]
        lab.log("warming up (one pass)")
        for _, path, params in requests:
            client.get(path, params=params)
        with lab.connect(args.database) as conn:
            conn.execute("SELECT pg_stat_statements_reset()")
        lab.log(f"running {args.clients} clients for {args.seconds:.0f} s")
        result = drive(requests, args.clients, args.seconds)
    lab.log(
        f"{result['requests']} requests, {result['errors']} errors, "
        f"{result['throughput_rps']} req/s"
    )
    top = top_statements(args.database)
    lab.log(f"database time total: {top['total_ms'] / 1000:.1f} s")
    for s in top["statements"]:
        lab.log(
            f"{s['share_percent']:>5}%  {s['total_ms'] / 1000:>7.1f} s  {s['calls']:>6} calls  "
            f"mean {s['mean_ms']:>9} ms  {s['query'][:100]}"
        )
    lab.save(
        f"e15_statements_{args.database}",
        {
            "load": {
                k: result[k]
                for k in (
                    "clients",
                    "requests",
                    "errors",
                    "throughput_rps",
                    "by_request",
                )
            },
            **top,
        },
    )


if __name__ == "__main__":
    main()
