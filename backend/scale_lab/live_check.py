"""Measure a hosted instance: server time, database time and queries per request, from the
`Server-Timing` header, plus response sizes. Read-only requests, sequential, with a throwaway
query parameter so the response cache cannot answer them.

Usage (from backend/):
    python -m scale_lab.live_check --url https://your-app.onrender.com --label after
Writes scale_lab/results/live_check_<label>.json. The first request is timed separately
because a free-tier service may need to wake up (30-60 s).
"""

import argparse
import re
import statistics
import time

import httpx

from scale_lab import lab

RUNS = 8


def median_of(rows: list, index: int):
    values = [row[index] for row in rows if row[index] is not None]
    return round(statistics.median(values)) if values else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="the site's address, without /api")
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    site = args.url.rstrip("/")
    api = site + "/api"
    client = httpx.Client(timeout=120, headers={"Accept-Encoding": "gzip"})

    started = time.perf_counter()
    status = client.get(api + "/departments").status_code
    first_ms = round((time.perf_counter() - started) * 1000)
    lab.log(f"first request: {first_ms} ms (status {status}); may include a wake-up")
    first_id = client.get(api + "/employees?limit=1").json()["items"][0]["id"]

    counter = [0]

    def fresh(path: str) -> str:
        counter[0] += 1
        return (
            f"{api}{path}{'&' if '?' in path else '?'}_probe={args.label}{counter[0]}"
        )

    endpoints = {
        "reference list": "/departments",
        "directory page": "/employees?limit=50",
        "directory search": "/employees?q=smith&limit=50",
        "employee profile": f"/employees/{first_id}",
        "pay insight: summary": "/analytics/summary",
        "pay insight: outliers": "/analytics/outliers",
        "pay insight: histogram": "/analytics/histogram",
        "CSV export": "/employees/export",
    }
    results: dict = {"first_request_ms": first_ms}
    for name, path in endpoints.items():
        for _ in range(2):
            client.get(fresh(path))
        rows = []
        for _ in range(RUNS):
            started = time.perf_counter()
            response = client.get(fresh(path))
            total = (time.perf_counter() - started) * 1000
            header = response.headers.get("server-timing", "")
            app = re.search(r"app;dur=([\d.]+)", header)
            db = re.search(r'db;dur=([\d.]+);desc="(\d+) queries', header)
            rows.append(
                (
                    total,
                    float(app.group(1)) if app else None,
                    float(db.group(1)) if db else None,
                    int(db.group(2)) if db else None,
                    response.num_bytes_downloaded,
                    response.headers.get("content-encoding"),
                )
            )

        results[name] = {
            "total_ms": median_of(rows, 0),
            "server_ms": median_of(rows, 1),
            "db_ms": median_of(rows, 2),
            "queries": rows[0][3],
            "bytes": rows[0][4],
            "encoding": rows[0][5],
        }
        lab.log(f"{name:24} {results[name]}")

    page = client.get(site + "/")
    bundle_path = re.search(r'/assets/[^"]+\.js', page.text).group(0)
    started = time.perf_counter()
    bundle = client.get(site + bundle_path)
    results["bundle"] = {
        "total_ms": round((time.perf_counter() - started) * 1000),
        "bytes": bundle.num_bytes_downloaded,
        "encoding": bundle.headers.get("content-encoding"),
        "cache_control": bundle.headers.get("cache-control"),
    }
    lab.log(f"bundle: {results['bundle']}")
    results["url"], results["runs_each"] = site, RUNS
    lab.save(f"live_check_{args.label}", results)


if __name__ == "__main__":
    main()
