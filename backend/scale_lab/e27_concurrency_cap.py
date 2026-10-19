"""E27: a cap on concurrent pay-insight requests.

The Pay insights page asks for its seven views at once. This starts the API with different
HEAVY_CONCURRENCY settings and loads that page like a browser does (all seven together), once
and then from several users at the same moment, with the response cache off. For each cap it
records the time until every view of a page has arrived and the time of the first and the
slowest view.

Usage (from backend/):
    python -m scale_lab.e27_concurrency_cap --database acme_salary --label laptop
Limit the database CPU first (docker update --cpus 0.5 <container>) to imitate a small
server, and put it back afterwards.
"""

import argparse
import os
import statistics
import subprocess
import sys
import threading
import time
from datetime import timedelta

import httpx

from app.services.compensation import utc_today
from scale_lab import lab

PORT = 8765
CAPS = (0, 4, 2, 1)  # 0: no cap
VIEWS = (
    "/analytics/summary",
    "/analytics/stats",
    "/analytics/role-by-country",
    "/analytics/histogram",
    "/analytics/outliers",
    "/analytics/composition",
    "/analytics/changes",
)


def start_server(database: str, cap: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": lab.lab_url(database),
        "RUNTIME_DATABASE_URL": "",
        "HEAVY_CONCURRENCY": str(cap),
        "HEAVY_WAIT_SECONDS": "300",
        "ANALYTICS_CACHE_SECONDS": "0",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=lab.BACKEND,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/departments", timeout=2).is_success:
                return server
        except httpx.HTTPError:
            time.sleep(0.2)
    server.terminate()
    raise RuntimeError("the server did not start")


def page_load(client: httpx.Client, results: list) -> None:
    """One browser opening the page: every view requested at the same moment."""
    today = utc_today()
    out = [None] * len(VIEWS)

    def get(i: int, path: str) -> None:
        params = {"_": str(time.perf_counter())}
        if path.endswith("changes"):
            params |= {"date_from": today - timedelta(days=30), "date_to": today}
        started = time.perf_counter()
        status = client.get(path, params=params).status_code
        out[i] = ((time.perf_counter() - started) * 1000, status)

    threads = [threading.Thread(target=get, args=(i, p)) for i, p in enumerate(VIEWS)]
    started = time.perf_counter()
    [t.start() for t in threads]
    [t.join() for t in threads]
    results.append(
        {
            "page_ms": (time.perf_counter() - started) * 1000,
            "view_ms": [ms for ms, _ in out],
            "errors": sum(1 for _, status in out if status != 200),
        }
    )


def measure(users: int, rounds: int) -> dict:
    client = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=600)
    page_load(client, [])  # warm-up
    pages: list[dict] = []
    wall = []
    for _ in range(rounds):
        started = time.perf_counter()
        threads = [
            threading.Thread(target=page_load, args=(client, pages)) for _ in range(users)
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        wall.append((time.perf_counter() - started) * 1000)
    views = [ms for p in pages for ms in p["view_ms"]]
    return {
        "users": users,
        "page_ms_median": round(statistics.median(p["page_ms"] for p in pages)),
        "page_ms_max": round(max(p["page_ms"] for p in pages)),
        "view_ms_median": round(statistics.median(views)),
        "view_ms_fastest": round(min(views)),
        "everyone_done_ms_median": round(statistics.median(wall)),
        "errors": sum(p["errors"] for p in pages),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_salary")
    parser.add_argument("--label", required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    results: dict = {"environment": lab.environment(), "caps": {}}
    for cap in CAPS:
        server = start_server(args.database, cap)
        try:
            out = {}
            for users in (1, 3):
                out[f"{users}_users"] = measure(users, args.rounds)
                lab.log(f"cap {cap or 'off'}: {out[f'{users}_users']}")
            results["caps"][str(cap or "off")] = out
        finally:
            server.terminate()
            server.wait()
    lab.save(f"e27_concurrency_cap_{args.label}", results)


if __name__ == "__main__":
    main()
