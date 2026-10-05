"""Phase 5 acceptance: directory p95 latency over a mix of search/filter/sort requests.

Usage (from backend/):
    python -m scale_lab.directory_latency [--runs 20]

Runs against whatever DATABASE_URL points at (the 10k dev database by default; set
DATABASE_URL to a lab database for 1M). Requests go through the full FastAPI stack.
"""

import argparse
import statistics
import time

from fastapi.testclient import TestClient

from app.main import app
from scale_lab import lab

TARGET_P95_MS = 500

MIX: list[dict] = [
    {},
    {"sort": "hire_date", "order": "desc"},
    {"sort": "compensation", "order": "desc"},
    {"sort": "compensation", "order": "asc", "reporting_currency": "INR"},
    {"department": "Engineering"},
    {"country": "IN", "status": "active"},
    {"country": ["US", "GB"], "job_level": "L4", "sort": "hire_date"},
    {"department": "Sales", "sort": "compensation", "order": "desc"},
    {"job_title": "Software Engineer", "country": "DE"},
    {"q": "an"},
    {"q": "kumar", "country": "IN"},
    {"q": "EMP-0012"},
    {"q": "zzzz-no-match"},
    {"status": "terminated", "sort": "hire_date", "order": "desc"},
    {"limit": 200},
]


def p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=100, method="inclusive")[94]


def run(runs: int) -> dict:
    client = TestClient(app)
    per_query, everything = [], []
    for params in MIX:
        first = client.get("/employees", params=params).json()  # warm-up
        samples = []
        for i in range(runs):
            # Alternate first pages with second pages, so cursor reads are measured too.
            query = dict(params)
            if i % 2 and first.get("next_cursor"):
                query["cursor"] = first["next_cursor"]
            started = time.perf_counter()
            response = client.get("/employees", params=query)
            samples.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 200, response.text
        everything += samples
        per_query.append(
            {
                "params": params,
                "median_ms": round(statistics.median(samples), 1),
                "p95_ms": round(p95(samples), 1),
            }
        )
        lab.log(
            f"{params!s:<75} median {per_query[-1]['median_ms']:>7} ms  p95 {per_query[-1]['p95_ms']:>7} ms"
        )
    overall = round(p95(everything), 1)
    lab.log(
        f"overall p95: {overall} ms over {len(everything)} requests (target < {TARGET_P95_MS} ms)"
    )
    return {
        "overall_p95_ms": overall,
        "requests": len(everything),
        "queries": per_query,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--label", default="dev_10k")
    args = parser.parse_args()
    lab.save(f"directory_latency_{args.label}", run(args.runs))


if __name__ == "__main__":
    main()
