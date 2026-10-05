"""Phase 14: the 500 ms check (REQUIREMENTS §1, §7) over the whole read API, not just the first
directory pages: realistic combinations of search, filters and sorts, the CSV export, and
every pay-insight view, each timed through the full FastAPI stack.

Usage (from backend/):
    python -m scale_lab.api_latency [--runs 20] [--label dev_10k]

Runs against whatever DATABASE_URL points at (the 10k dev database by default) and expects
exchange rates to be stored (sorting by pay and every conversion read them).
"""

import argparse
import statistics
import time

from fastapi.testclient import TestClient

from app.main import app
from scale_lab import lab
from scale_lab.directory_latency import p95, resolve

TARGET_P95_MS = 500

# Realistic: what HR actually does. Names are resolved to ids like the UI does.
DIRECTORY: list[dict] = [
    {},
    {"q": "an"},
    {"q": "smith", "status": ["active", "on_leave"], "sort": "compensation"},
    {
        "department": "Engineering",
        "country": "IN",
        "job_level": "L3",
        "status": "active",
        "sort": "compensation",
        "order": "desc",
    },
    {"q": "an", "department": "Sales", "sort": "hire_date"},
    {
        "min_level_rank": 4,
        "country": ["US", "GB", "DE"],
        "sort": "level",
        "order": "desc",
    },
    {
        "job_title": "Support Specialist",
        "min_level_rank": 2,
        "max_level_rank": 4,
        "reporting_currency": "EUR",
        "sort": "compensation",
    },
    {
        "country": ["IN", "US", "GB", "DE", "BR"],
        "status": "active",
        "sort": "compensation",
        "order": "desc",
        "limit": 200,
    },
    {"status": "terminated", "sort": "hire_date", "order": "desc"},
    {"q": "EMP-0012"},
    {"q": "zzzz-no-match"},
]
EXPORT: list[dict] = [
    {},
    {"country": "IN", "sort": "compensation", "order": "desc"},
    {"department": "Sales", "job_level": "L3", "reporting_currency": "EUR"},
]
ANALYTICS: list[tuple[str, dict]] = [
    ("summary", {}),
    ("stats", {"group_by": "level"}),
    ("stats", {"group_by": "title"}),
    ("role-by-country", {"job_title": "Software Engineer", "job_level": "L3"}),
    ("histogram", {"bins": 20}),
    ("outliers", {}),
    ("composition", {}),
    ("changes", {"date_from": "2025-01-01", "date_to": "2025-12-31"}),
    (
        "summary",
        {"department": "Engineering", "country": "IN", "reporting_currency": "INR"},
    ),
    ("outliers", {"country": "US", "reporting_currency": "EUR"}),
]


def measure(call, runs: int, pages: bool = False) -> list[float]:
    first = call(None)  # warm-up
    samples = []
    for i in range(runs):
        cursor = (
            first.get("next_cursor") if pages and i % 2 else None
        )  # alternate first and second pages
        started = time.perf_counter()
        call(cursor)
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def run(runs: int) -> dict:
    client = TestClient(app)
    results: dict[str, list[dict]] = {"directory": [], "export": [], "analytics": []}
    everything: dict[str, list[float]] = {key: [] for key in results}

    def record(group: str, label: str, samples: list[float]) -> None:
        everything[group] += samples
        row = {
            "request": label,
            "median_ms": round(statistics.median(samples), 1),
            "p95_ms": round(p95(samples), 1),
        }
        results[group].append(row)
        lab.log(
            f"{group:9} {label:<110} median {row['median_ms']:>7} ms  p95 {row['p95_ms']:>7} ms"
        )

    for named in DIRECTORY:
        params = resolve(client, named)

        def get_page(cursor, params=params):
            response = client.get(
                "/employees",
                params={**params, **({"cursor": cursor} if cursor else {})},
            )
            assert response.status_code == 200, response.text
            return response.json()

        record("directory", str(named), measure(get_page, runs, pages=True))
    for named in EXPORT:
        params = resolve(client, named)

        def export(cursor, params=params):
            response = client.get("/employees/export", params=params)
            assert response.status_code == 200, response.text
            return {}

        record("export", str(named), measure(export, max(runs // 2, 5)))
    for view, named in ANALYTICS:
        params = resolve(client, named)

        def analytics(cursor, view=view, params=params):
            response = client.get(f"/analytics/{view}", params=params)
            assert response.status_code == 200, response.text
            return {}

        record("analytics", f"{view} {named}", measure(analytics, max(runs // 2, 5)))

    summary = {group: round(p95(samples), 1) for group, samples in everything.items()}
    for group, value in summary.items():
        verdict = "within" if value < TARGET_P95_MS else "OVER"
        lab.log(
            f"overall p95 {group:9} {value:>8} ms ({verdict} the {TARGET_P95_MS} ms target; {len(everything[group])} requests)"
        )
    return {
        "target_ms": TARGET_P95_MS,
        "overall_p95_ms": summary,
        "requests": {k: len(v) for k, v in everything.items()},
        **results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--label", default="dev_10k")
    args = parser.parse_args()
    lab.save(f"api_latency_{args.label}", run(args.runs))


if __name__ == "__main__":
    main()
