"""E21: what gzip and cache headers do for the hosted app, on the 10,000-employee database.

Starts two copies of the real site (the built frontend plus the API): one with compression
off, one on. Compares bytes, CPU cost and download time at a slow and a fast link (curl's
--limit-rate), and times gzip levels on the real payloads.

Usage (from backend/, after `npm run build` in frontend/):
    python -m scale_lab.e21_compression [--database acme_salary]
"""

import argparse
import gzip
import os
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from scale_lab import lab

DIST = Path(lab.BACKEND).parent / "frontend" / "dist"
OFF, ON = 8903, 8904
LINKS = {"4 Mbps": "500K", "100 Mbps": "12500K"}  # curl --limit-rate, bytes per second


@contextmanager
def site(port: int, database: str, compress: bool):
    env = {
        **os.environ,
        "DATABASE_URL": lab.lab_url(database),
        "RUNTIME_DATABASE_URL": "",
        "ENVIRONMENT": "production",
        "ANALYTICS_CACHE_SECONDS": "0",
        "COMPRESS_RESPONSES": "true" if compress else "false",
        "FRONTEND_DIST": str(DIST),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.web:site",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=lab.BACKEND,
        env=env,
    )
    try:
        for _ in range(120):
            try:
                if (
                    httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=30)


def targets(base: str) -> dict[str, str]:
    html = httpx.get(base + "/").text
    assets = [p for p in html.replace('"', " ").split() if p.startswith("/assets/")]
    js = next(a for a in assets if a.endswith(".js"))
    css = next(a for a in assets if a.endswith(".css"))
    return {
        "JavaScript bundle": js,
        "stylesheet": css,
        "reference: countries": "/api/countries",
        "directory page (50)": "/api/employees?limit=50",
        "pay insight: outliers": "/api/analytics/outliers",
        "CSV export (all 10k)": "/api/employees/export",
    }


def fetch(base: str, path: str, gzip_ok: bool) -> tuple[int, float]:
    headers = {"Accept-Encoding": "gzip" if gzip_ok else "identity"}
    with httpx.Client(timeout=120) as client:
        started = time.perf_counter()
        response = client.get(base + path, headers=headers)
        elapsed = (time.perf_counter() - started) * 1000
        return response.num_bytes_downloaded, elapsed


def throttled_seconds(base: str, path: str, rate: str, compressed: bool) -> float:
    cmd = [
        "curl",
        "-s",
        "-o",
        "/dev/null",
        "-w",
        "%{time_total}",
        "--limit-rate",
        rate,
        base + path,
    ]
    if compressed:
        cmd.insert(1, "--compressed")
    return float(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_salary")
    args = parser.parse_args()
    results: dict = {}
    with site(OFF, args.database, False) as off, site(ON, args.database, True) as on:
        paths = targets(on)
        for name, path in paths.items():
            for _ in range(3):  # warm-up
                fetch(on, path, True)
                fetch(off, path, False)
            plain_bytes = fetch(off, path, False)[0]
            gz_bytes = fetch(on, path, True)[0]
            plain_ms = statistics.median(fetch(off, path, False)[1] for _ in range(10))
            gz_ms = statistics.median(fetch(on, path, True)[1] for _ in range(10))
            row = {
                "path": path,
                "bytes_plain": plain_bytes,
                "bytes_gzip": gz_bytes,
                "ratio": round(plain_bytes / gz_bytes, 1),
                "unthrottled_ms_plain": round(plain_ms, 1),
                "unthrottled_ms_gzip": round(gz_ms, 1),
                "links": {},
            }
            for link, rate in LINKS.items():
                plain_s = statistics.median(
                    throttled_seconds(off, path, rate, False) for _ in range(3)
                )
                gz_s = statistics.median(
                    throttled_seconds(on, path, rate, True) for _ in range(3)
                )
                row["links"][link] = {
                    "plain_ms": round(plain_s * 1000),
                    "gzip_ms": round(gz_s * 1000),
                }
            results[name] = row
            lab.log(
                f"{name:24} {plain_bytes:>9,} B -> {gz_bytes:>8,} B ({row['ratio']}x)  "
                f"local {row['unthrottled_ms_plain']} -> {row['unthrottled_ms_gzip']} ms  "
                + "  ".join(
                    f"{k}: {v['plain_ms']} -> {v['gzip_ms']} ms"
                    for k, v in row["links"].items()
                )
            )
        # gzip levels on the real payloads
        levels = {}
        for name in (
            "JavaScript bundle",
            "directory page (50)",
            "CSV export (all 10k)",
        ):
            raw = httpx.get(
                off + paths[name], headers={"Accept-Encoding": "identity"}
            ).content
            levels[name] = {}
            for level in (1, 5, 6, 9):
                times = []
                for _ in range(5):
                    started = time.perf_counter()
                    packed = gzip.compress(raw, compresslevel=level)
                    times.append((time.perf_counter() - started) * 1000)
                levels[name][str(level)] = {
                    "bytes": len(packed),
                    "ms": round(statistics.median(times), 2),
                }
            lab.log(f"gzip levels, {name}: {levels[name]}")
        results["gzip_levels"] = levels
        # cache headers for repeat visits
        js = paths["JavaScript bundle"]
        first = httpx.get(on + js, headers={"Accept-Encoding": "gzip"})
        revalidate = httpx.get(
            on + js, headers={"If-None-Match": first.headers["etag"]}
        )
        results["cache"] = {
            "cache_control": first.headers.get("cache-control"),
            "etag_revalidation_status": revalidate.status_code,
            "etag_revalidation_bytes": revalidate.num_bytes_downloaded,
        }
        lab.log(f"cache headers: {results['cache']}")
    lab.save(f"e21_compression_{args.database}", results)


if __name__ == "__main__":
    main()
