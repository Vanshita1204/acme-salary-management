"""E20: max_parallel_workers_per_gather and effective_cache_size at 1M employees.

Sets each value on the lab database (ALTER DATABASE ... SET, so new connections pick it up),
measures single-client latency in-process and ten-client directory throughput through the
real server, and resets the settings at the end.

Usage (from backend/):
    python -m scale_lab.e20_planner_settings [--database acme_lab_1m]
"""

import argparse
import statistics
import time

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services import analytics as a
from app.services import directory as d
from scale_lab import lab
from scale_lab.e9_concurrency import BASE, DIRECTORY, drive, resolve, server

VIEWS = {
    "summary": lambda s: a.summary(s, d.DirectoryQuery()),
    "stats by department": lambda s: a.grouped_stats(
        s, d.DirectoryQuery(), "department"
    ),
    "role across countries": lambda s: a.role_by_country(s, d.DirectoryQuery()),
    "histogram": lambda s: a.histogram(s, d.DirectoryQuery(), 20),
    "composition": lambda s: a.composition(s, d.DirectoryQuery()),
    "outliers": lambda s: a.outliers(s, d.DirectoryQuery()),
}
PAGES = {
    "directory first page": d.DirectoryQuery(),
    "directory department + country": d.DirectoryQuery(q=None, countries=["IN"]),
    "directory search 'abbott'": d.DirectoryQuery(q="abbott"),
}
WORKERS = [0, 2, 4, 8]
CACHE_SIZES = ["1GB", "4GB", "6GB"]


def timed(engine, call, runs: int) -> float:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        with Session(engine) as session:
            call(session)
            session.commit()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def measure(database: str) -> dict:
    engine = create_engine(
        lab.lab_url(database), future=True, connect_args={"prepare_threshold": None}
    )
    out: dict = {"views_ms": {}, "pages_ms": {}}
    for name, call in VIEWS.items():
        timed(engine, call, 1)  # warm-up
        out["views_ms"][name] = round(min(timed(engine, call, 3)))
    for name, query in PAGES.items():
        call = lambda s, q=query: d.list_employees(s, q)
        timed(engine, call, 1)
        out["pages_ms"][name] = round(statistics.median(timed(engine, call, 5)), 1)
    engine.dispose()
    with server(database, 1, 5, 10), httpx.Client(base_url=BASE, timeout=120) as client:
        requests = [(lbl, p, resolve(client, prm)) for lbl, p, prm in DIRECTORY]
        for _, path, params in requests:
            client.get(path, params=params)
        row = drive(requests, 10, 12)
    out["ten_clients"] = {
        k: row[k] for k in ("throughput_rps", "p50_ms", "p95_ms", "errors")
    }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_lab_1m")
    args = parser.parse_args()
    results: dict = {"max_parallel_workers_per_gather": {}, "effective_cache_size": {}}
    try:
        with lab.connect("postgres") as admin:
            for workers in WORKERS:
                admin.execute(
                    f"ALTER DATABASE {args.database} SET max_parallel_workers_per_gather = {workers}"
                )
                row = measure(args.database)
                results["max_parallel_workers_per_gather"][str(workers)] = row
                lab.log(f"workers={workers}: {row}")
            admin.execute(
                f"ALTER DATABASE {args.database} RESET max_parallel_workers_per_gather"
            )
            for size in CACHE_SIZES:
                admin.execute(
                    f"ALTER DATABASE {args.database} SET effective_cache_size = '{size}'"
                )
                row = measure(args.database)
                results["effective_cache_size"][size] = row
                lab.log(f"effective_cache_size={size}: {row}")
    finally:
        with lab.connect("postgres") as admin:
            admin.execute(f"ALTER DATABASE {args.database} RESET ALL")
    lab.save(f"e20_planner_settings_{args.database}", results)


if __name__ == "__main__":
    main()
