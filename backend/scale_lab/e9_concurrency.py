"""E9: how the API behaves as concurrent requests rise (connection pool, workers).

Starts the real server (uvicorn, its own process) against a lab database and drives it
with N concurrent clients, each sending requests back to back for a fixed time (a
closed loop: a slow server also slows the load, so latencies here are lower than an
open-loop test would show at the same offered rate). The response cache is switched off,
so every request does its work.

Usage (from backend/):
    python -m scale_lab.e9_concurrency --database acme_lab_1m --label baseline
        [--levels 1,5,10,20,50] [--duration 15]
        [--analytics-levels 1,2,4,8,16] [--analytics-duration 60] [--workers 1]
        [--pool-size 5] [--max-overflow 10] [--workloads directory,analytics]
"""

import argparse
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import httpx

from scale_lab import lab

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
WARMUP_SECONDS = 3

# (label, path, params). Ids are resolved by name at start, like the UI does.
DIRECTORY = [
    ("first page", "/employees", {"limit": 50}),
    ("search", "/employees", {"q": "an", "limit": 50}),
    (
        "filtered by department and country",
        "/employees",
        {"department": "Engineering", "country": "IN", "limit": 50},
    ),
    ("by hire date", "/employees", {"sort": "hire_date", "order": "desc", "limit": 50}),
]
ANALYTICS = [
    (
        "directory sorted by pay",
        "/employees",
        {
            "department": "Engineering",
            "country": "IN",
            "sort": "compensation",
            "order": "desc",
            "limit": 50,
        },
    ),
    ("summary", "/analytics/summary", {}),
    ("stats by department", "/analytics/stats", {"group_by": "department"}),
    ("composition", "/analytics/composition", {}),
    ("histogram", "/analytics/histogram", {"bins": 20}),
    ("outliers", "/analytics/outliers", {}),
]
WORKLOADS = {"directory": DIRECTORY, "analytics": ANALYTICS}
SERVER_TIME = re.compile(r"app;dur=([\d.]+)")


@contextmanager
def server(database: str, workers: int, pool_size: int, max_overflow: int):
    env = {
        **os.environ,
        "DATABASE_URL": lab.lab_url(database),
        "ANALYTICS_CACHE_SECONDS": "0",
        "DB_POOL_SIZE": str(pool_size),
        "DB_MAX_OVERFLOW": str(max_overflow),
    }
    env["RUNTIME_DATABASE_URL"] = ""  # empty beats a runtime URL in .env
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--workers",
            str(workers),
            "--log-level",
            "warning",
        ],
        cwd=lab.BACKEND,
        env=env,
    )
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            raise RuntimeError("server did not start")
        yield
    finally:
        process.terminate()
        process.wait(timeout=30)


def resolve(client: httpx.Client, params: dict) -> dict:
    out = dict(params)
    if "department" in out:
        departments = {d["name"]: d["id"] for d in client.get("/departments").json()}
        out["department_id"] = departments[out.pop("department")]
    return out


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))]


def drive(requests: list[tuple[str, str, dict]], clients: int, seconds: float) -> dict:
    """`clients` threads, each looping over the request list from a different offset."""
    samples: list[tuple[str, float, float | None, int]] = []
    lock = threading.Lock()
    stop = time.perf_counter() + seconds

    def worker(offset: int) -> None:
        mine: list[tuple[str, float, float | None, int]] = []
        with httpx.Client(base_url=BASE, timeout=120) as client:
            i = offset
            while time.perf_counter() < stop:
                label, path, params = requests[i % len(requests)]
                i += 1
                started = time.perf_counter()
                try:
                    response = client.get(path, params=params)
                    status = response.status_code
                    match = SERVER_TIME.search(
                        response.headers.get("server-timing", "")
                    )
                    server_ms = float(match.group(1)) if match else None
                except httpx.HTTPError:
                    status, server_ms = 0, None
                mine.append(
                    (label, (time.perf_counter() - started) * 1000, server_ms, status)
                )
        with lock:
            samples.extend(mine)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(clients)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started

    ok = [s for s in samples if s[3] == 200]
    latencies = [s[1] for s in ok]
    server_times = [s[2] for s in ok if s[2] is not None]
    by_request = {}
    for label in dict.fromkeys(r[0] for r in requests):
        values = [s[1] for s in ok if s[0] == label]
        if values:
            by_request[label] = {
                "n": len(values),
                "p50_ms": round(statistics.median(values), 1),
                "p95_ms": round(percentile(values, 95), 1),
            }
    return {
        "clients": clients,
        "requests": len(samples),
        "errors": len(samples) - len(ok),
        "error_statuses": {
            str(s): sum(1 for x in samples if x[3] == s)
            for s in sorted({x[3] for x in samples} - {200})
        },
        "throughput_rps": round(len(ok) / elapsed, 1),
        "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_ms": round(percentile(latencies, 95), 1) if latencies else None,
        "p99_ms": round(percentile(latencies, 99), 1) if latencies else None,
        "max_ms": round(max(latencies), 1) if latencies else None,
        "server_p50_ms": round(statistics.median(server_times), 1)
        if server_times
        else None,
        "by_request": by_request,
    }


def run(args) -> dict:
    results: dict[str, list[dict]] = {}
    with (
        server(args.database, args.workers, args.pool_size, args.max_overflow),
        httpx.Client(base_url=BASE, timeout=120) as client,
    ):
        for name in args.workloads.split(","):
            requests = [
                (label, path, resolve(client, params))
                for label, path, params in WORKLOADS[name]
            ]
            lab.log(f"{name}: warming up")
            for _, path, params in requests:  # once each, so caches are warm for all
                client.get(path, params=params)
            drive(requests, 1, WARMUP_SECONDS)
            results[name] = []
            # Seconds per request at 1M: the heavy views get fewer clients, a longer window.
            heavy = name == "analytics"
            levels_arg = args.analytics_levels if heavy else args.levels
            seconds = args.analytics_duration if heavy else args.duration
            for clients in (int(n) for n in levels_arg.split(",")):
                row = drive(requests, clients, seconds)
                results[name].append(row)
                lab.log(
                    f"{name:9} {clients:>3} clients  {row['throughput_rps']:>7} req/s  "
                    f"p50 {row['p50_ms']:>8} ms  p95 {row['p95_ms']:>8} ms  p99 {row['p99_ms']:>8} ms  "
                    f"server p50 {row['server_p50_ms']:>8} ms  errors {row['errors']} {row['error_statuses'] or ''}"
                )
    return {
        "database": args.database,
        "config": {
            "workers": args.workers,
            "pool_size": args.pool_size,
            "max_overflow": args.max_overflow,
            "duration_s": args.duration,
            "analytics_duration_s": args.analytics_duration,
            "cache": "off",
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--levels", default="1,5,10,20,50")
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument("--analytics-levels", default="1,2,4,8,16")
    parser.add_argument("--analytics-duration", type=float, default=60)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--pool-size", type=int, default=5)
    parser.add_argument("--max-overflow", type=int, default=10)
    parser.add_argument("--workloads", default="directory,analytics")
    args = parser.parse_args()
    lab.save(f"e9_concurrency_{args.label}", run(args))


if __name__ == "__main__":
    main()
