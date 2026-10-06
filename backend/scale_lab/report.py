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


def e4(database: str) -> None:
    data = load(f"e4_search_{database}")
    if not data:
        return
    print(f"### E4 — search, {data['employees']:,} employees, median ms\n")
    print("| Term | Matches | No index | Trigram GIN | Trigram GIN, driver default |")
    print("|---|---|---|---|---|")
    for term, plain in data["results"]["no_index"].items():
        gin = data["results"]["trigram_gin"][term]
        default = (
            data["results"]
            .get("trigram_gin_driver_default", {})
            .get(term, {})
            .get("median_ms", "n/a")
        )
        print(
            f"| {term} | {plain['matches']:,} | {plain['median_ms']} | {gin['median_ms']} | {default} |"
        )
    print()
    print("| Last-name prefix | No index | B-tree prefix |")
    print("|---|---|---|")
    for term, plain in data["results"]["no_index_prefix_query"].items():
        print(
            f"| {term} | {plain['median_ms']} | {data['results']['btree_prefix'][term]['median_ms']} |"
        )
    tri, bt = data["trigram_indexes"], data["btree_prefix_index"]
    mb = lambda b: f"{b / 2**20:.1f} MB"
    print(
        f"\nIndexes: B-tree prefix {mb(bt['bytes'])}, {bt['build_seconds']} s; "
        f"four trigram GIN {mb(tri['total_bytes'])}, {tri['total_build_seconds']} s. "
        f"Result mismatches between plain and trigram: {data['result_mismatches'] or 'none'}.\n"
    )


def e5(database: str) -> None:
    data = load(f"e5_analytics_{database}")
    if not data:
        return
    print(f"### E5 — analytics, {database}, median ms\n")
    print("| View | Live | Materialized view | Same answer |")
    print("|---|---|---|---|")
    for name, r in data["results"].items():
        print(
            f"| {name} | {r['live']['median_ms']} | {r['materialized']['median_ms']} | {r['same_answer']} |"
        )
    print(
        f"\nView: {data['view_rows']:,} rows, {float(data['view_bytes']) / 2**20:.1f} MB, "
        f"built in {data['build_seconds']} s.\n"
    )
    print("| Changed employees | Plain refresh (s) | Concurrent refresh (s) |")
    print("|---|---|---|")
    for batch, r in data["refresh_after_changes"].items():
        print(f"| {int(batch):,} | {r['plain_seconds']} | {r['concurrent_seconds']} |")
    print()


def e6(database: str) -> None:
    data = load(f"e6_import_{database}")
    if not data:
        return
    print(
        f"### E6 — CSV import, {database} ({data['employees_before']:,} employees already present)\n"
    )
    print("| Rows | File | Path | Seconds | Peak memory |")
    print("|---|---|---|---|---|")
    for rows, entry in data["results"].items():
        if rows == "rejected_file":
            continue
        for path in ("application", "staging", "staging_work_mem_256mb"):
            if path in entry:
                r = entry[path]
                print(
                    f"| {int(rows):,} | {entry['file_mb']} MB | {path} | {r['seconds']} | {r['peak_rss_mb']} MB |"
                )
    rej = data["results"]["rejected_file"]
    print(
        f"\nFile with 3 bad rows: {rej['outcome']} after {rej['seconds']} s, "
        f"{rej['problems_reported']} problems reported; employees before/after: "
        f"{data['employees_before']:,} / {data['employees_after']:,}.\n"
    )


def e7(employees: int) -> None:
    data = load(f"e7_index_cost_{employees}")
    if not data:
        return
    configs = data["results"]
    print(f"### E7 — index write cost, {employees:,} employees\n")
    print("| | " + " | ".join(configs) + " |")
    print("|---|" + "---|" * len(configs))
    print(
        "| Secondary indexes | "
        + " | ".join(str(c["secondary_indexes"]) for c in configs.values())
        + " |"
    )
    print(
        "| Index size | "
        + " | ".join(
            f"{float(c['index_bytes']) / 2**20:.0f} MB" for c in configs.values()
        )
        + " |"
    )
    print(
        "| Bulk load (s) | "
        + " | ".join(str(c["load_seconds"]) for c in configs.values())
        + " |"
    )
    for key, label in (
        ("record_insert_median_ms", "Record insert (ms)"),
        ("employee_insert_median_ms", "Employee insert (ms)"),
    ):
        print(
            f"| {label} | "
            + " | ".join(str(c["inserts"][key]) for c in configs.values())
            + " |"
        )
    for query in next(iter(configs.values()))["reads_ms"]:
        print(
            f"| {query} (ms) | "
            + " | ".join(str(c["reads_ms"][query]) for c in configs.values())
            + " |"
        )
    print()


def e8(database: str) -> None:
    data = load(f"e8_partitioning_{database}")
    if not data:
        return
    print(
        f"### E8 — partitioning, {database}, {data['records']:,} records, {data['partitions']} partitions, median ms\n"
    )
    print("| Query | Plain | Plain + date index | Partitioned |")
    print("|---|---|---|---|")
    for name, r in data["results"].items():
        print(
            f"| {name} | {r['plain']['median_ms']} | {r['plain_date_index']['median_ms']} | {r['partitioned']['median_ms']} |"
        )
    print(
        f"\nSingle-row insert: {data['single_insert_median_ms']} ms. Size: {data['total_bytes']}. Limits: {data['structural_limits']}\n"
    )


def main() -> None:
    e1()
    for database in ("acme_lab_s", "acme_lab_m"):
        e2(database)
        e3(database)
        e4(database)
        e5(database)
        e6(database)
        e8(database)
    e7(100_000)


if __name__ == "__main__":
    main()
