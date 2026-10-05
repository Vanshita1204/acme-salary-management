"""Build the lab datasets used by E2/E3 (and E1's 1M measurements).

Usage (from backend/):
    python -m scale_lab.build --size s|m [--method copy] [--database NAME]
"""

import argparse

from scale_lab import lab

SIZES = {"s": 10_000, "m": 1_000_000}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", choices=SIZES, required=True)
    parser.add_argument("--method", default="copy")
    parser.add_argument("--database")
    args = parser.parse_args()
    database = args.database or f"acme_lab_{args.size}"
    count = SIZES[args.size]

    lab.timed(
        f"{database}: schema + reference data", lambda: lab.prepare_reference(database)
    )
    seconds = lab.seed(database, count, args.method)
    lab.log(
        f"{database}: seeded {count:,} employees with {args.method} in {seconds:.1f}s"
    )
    lab.timed(f"{database}: ANALYZE", lambda: lab.analyze(database))
    with lab.connect(database) as conn:
        sizes = lab.table_sizes(conn)
    lab.save(
        f"build_{database}_{args.method}",
        {
            "database": database,
            "count": count,
            "method": args.method,
            "seed_seconds": round(seconds, 1),
            "sizes": sizes,
        },
    )
    lab.log(f"{database}: {sizes}")


if __name__ == "__main__":
    main()
