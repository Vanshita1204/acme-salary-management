"""E22: the CSV export, in-process, with a byte-for-byte check against saved output.

Times `export_employees` + `directory_csv` for several kinds of export (median of 3 runs, a
fresh session each) and prints a hash of each file. `--save DIR` writes the files; `--compare DIR`
checks them against files saved earlier (for example by the code before a change).

Usage (from backend/):
    python -m scale_lab.e22_export --database acme_salary [--save DIR | --compare DIR]
    python -m scale_lab.e22_export --database acme_lab_1m --cases singapore
"""

import argparse
import hashlib
import statistics
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services import directory as d
from app.services import exports
from scale_lab import lab

CASES = {
    "all": d.DirectoryQuery(),
    "india": d.DirectoryQuery(countries=["IN"]),
    "by_pay_desc": d.DirectoryQuery(
        sort="compensation", order="desc", countries=["US", "GB"]
    ),
    "by_level_eur": d.DirectoryQuery(
        sort="level", reporting_currency="EUR", countries=["DE"]
    ),
    "singapore": d.DirectoryQuery(countries=["SG"]),
    "search": d.DirectoryQuery(q="an", sort="hire_date", order="desc"),
    "active_only_inr": d.DirectoryQuery(
        statuses=["active"], reporting_currency="INR", countries=["IN", "SG"]
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_salary")
    parser.add_argument("--cases", help="comma-separated names; default all")
    parser.add_argument("--save", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    engine = create_engine(
        lab.lab_url(args.database),
        future=True,
        connect_args={"prepare_threshold": None},
    )
    names = args.cases.split(",") if args.cases else list(CASES)
    results = {}
    for name in names:
        times = []
        for _ in range(3):
            with Session(engine) as session:
                started = time.perf_counter()
                text = exports.directory_csv(d.export_employees(session, CASES[name]))
                times.append((time.perf_counter() - started) * 1000)
                session.commit()
        digest = hashlib.sha256(text.encode()).hexdigest()[:12]
        line = f"{name:16} rows {text.count(chr(10)) - 1:>7,}  {statistics.median(times):8.0f} ms  sha {digest}"
        if args.save:
            args.save.mkdir(parents=True, exist_ok=True)
            (args.save / f"{name}.csv").write_bytes(text.encode("utf-8"))
        if args.compare:
            # Bytes, not text: the CSV uses \r\n line endings, which read_text would rewrite.
            same = (args.compare / f"{name}.csv").read_bytes() == text.encode("utf-8")
            line += "  identical" if same else "  DIFFERENT"
        lab.log(line)
        results[name] = {
            "rows": text.count("\n") - 1,
            "median_ms": round(statistics.median(times)),
            "sha": digest,
        }
    lab.save(f"e22_export_{args.database}", results)


if __name__ == "__main__":
    main()
