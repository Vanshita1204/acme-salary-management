"""E18: ways of counting active employees by currency among those with pay that counts
toward the total (the pay-insight context query), at 1M employees.

Every variant must return the same counts. Median of 5 after a warm-up, unfiltered and with
the department and country filters the directory offers.

Usage (from backend/):
    python -m scale_lab.e18_currency_usage [--database acme_lab_1m]
"""

import argparse
import statistics
import time

from scale_lab import lab

FILTERS = {
    "unfiltered": "",
    "department + country": (
        "AND e.department_id = (SELECT id FROM departments WHERE name = 'Engineering') "
        "AND e.current_country = 'IN'"
    ),
}


def variants(counted_ids: list[int], extra: str) -> dict[str, str]:
    ids = ", ".join(map(str, counted_ids))
    base = "SELECT e.currency, count(*) FROM employees e"
    group = "GROUP BY e.currency ORDER BY 1"
    return {
        "V0 as built: EXISTS over pay joined to types": f"""
            {base} WHERE e.status = 'active' {extra} AND EXISTS (
              SELECT 1 FROM current_compensation cc
              JOIN compensation_types ct ON ct.id = cc.compensation_type_id
              WHERE cc.employee_id = e.id AND ct.counts_toward_total) {group}""",
        "V1 EXISTS with the literal list of counted type ids": f"""
            {base} WHERE e.status = 'active' {extra} AND EXISTS (
              SELECT 1 FROM current_compensation cc
              WHERE cc.employee_id = e.id AND cc.compensation_type_id IN ({ids})) {group}""",
        "V2 join to the stored totals": f"""
            {base} JOIN employee_totals t ON t.employee_id = e.id
            WHERE e.status = 'active' {extra} {group}""",
        "V3 EXISTS on the stored totals": f"""
            {base} WHERE e.status = 'active' {extra} AND EXISTS (
              SELECT 1 FROM employee_totals t WHERE t.employee_id = e.id) {group}""",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_lab_1m")
    args = parser.parse_args()
    results: dict = {}
    with lab.connect(args.database) as conn:
        counted = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM compensation_types WHERE counts_toward_total ORDER BY id"
            ).fetchall()
        ]
        for filter_name, extra in FILTERS.items():
            results[filter_name] = {}
            answers = {}
            for name, sql in variants(counted, extra).items():
                answers[name] = conn.execute(sql).fetchall()  # warm-up and answer
                samples = []
                for _ in range(5):
                    started = time.perf_counter()
                    conn.execute(sql).fetchall()
                    samples.append((time.perf_counter() - started) * 1000)
                results[filter_name][name] = {
                    "median_ms": round(statistics.median(samples), 1)
                }
                lab.log(
                    f"{filter_name:22} {name:55} {results[filter_name][name]['median_ms']:>8} ms"
                )
            reference = next(iter(answers.values()))
            same = all(a == reference for a in answers.values())
            results[filter_name]["identical_results"] = same
            lab.log(
                f"{filter_name:22} identical results across variants: {same} "
                f"({len(reference)} currencies, {sum(c for _, c in reference):,} employees)"
            )
    lab.save(f"e18_currency_usage_{args.database}", results)


if __name__ == "__main__":
    main()
