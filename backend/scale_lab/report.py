"""Print Markdown tables from the Scale Lab result files, for docs/PERFORMANCE.md.

Usage (from backend/):
    python -m scale_lab.report
"""

import json

from scale_lab.lab import RESULTS


def load(name: str) -> dict | None:
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def plan_summary(plan: list[str]) -> str:
    """The first plan node plus any scan/sort lines — enough to say *how* it ran."""
    keep = [plan[0].strip()]
    for line in plan[1:]:
        text = line.strip().lstrip("->").strip()
        if any(
            k in text
            for k in (
                "Scan",
                "Sort",
                "Unique",
                "WindowAgg",
                "HashAggregate",
                "GroupAggregate",
            )
        ):
            keep.append(text.split("  (")[0])
    return "; ".join(dict.fromkeys(keep[:6]))


def e1() -> None:
    print("### E1 — bulk loading\n")
    print("| Employees | Method | Seconds | Rows/s | Records |")
    print("|---|---|---|---|---|")
    for name in ("e1_loading_10000",):
        data = load(name)
        if data:
            for method, r in data["methods"].items():
                print(
                    f"| {data['count']:,} | {method} | {r['seconds']} | {r['rows_per_second']:,} | {r['records']:,} |"
                )
    for name in ("build_acme_lab_m_copy", "build_acme_lab_e1m_batch"):
        data = load(name)
        if data:
            records = data["sizes"]["compensation_records"]["rows"]
            rows = data["count"] + records
            print(
                f"| {data['count']:,} | {data['method']} | {data['seed_seconds']} | "
                f"{round(rows / data['seed_seconds']):,} | {records:,} |"
            )
    print()


def e2(database: str) -> None:
    data = load(f"e2_pagination_{database}")
    if not data:
        return
    print(
        f"### E2 — pagination, {data['employees']:,} employees (page of {data['page_size']}), median ms\n"
    )
    print("| Sort | Method | First | Middle | Last |")
    print("|---|---|---|---|---|")
    for sort, results in data["results"].items():
        for method in ("offset", "keyset"):
            cells = [
                str(results[f"{method}/{p}"]["median_ms"])
                for p in ("first", "middle", "last")
            ]
            print(f"| {sort} | {method} | {' | '.join(cells)} |")
    print()


def e3(database: str) -> None:
    data = load(f"e3_current_compensation_{database}")
    if not data:
        return
    print(f"### E3 — current compensation, {database}, median ms\n")
    print("| Workload | Strategy | With index | Without index |")
    print("|---|---|---|---|")
    with_index, without = (
        data["results"]["with_index"],
        data["results"]["without_index"],
    )
    for key, result in with_index.items():
        workload, strategy = key.split("/")
        other = without.get(key, {}).get("median_ms", "n/a")
        print(f"| {workload} | {strategy} | {result['median_ms']} | {other} |")
    print()
    print("Plans (with index):\n")
    for key, result in with_index.items():
        print(f"- `{key}`: {plan_summary(result['plan'])}")
    print()


def main() -> None:
    e1()
    for database in ("acme_lab_s", "acme_lab_m"):
        e2(database)
        e3(database)


if __name__ == "__main__":
    main()
