"""E13: the directory's text search at 1M employees, with and without trigram indexes.

Times a whole directory request (fresh session per request, like the API) for terms of
different lengths and selectivity, and a bulk insert of 100,000 employees (rolled back), so
the same script gives the "before" and the "after" depending on which indexes exist.

Usage (from backend/):
    python -m scale_lab.e13_trigram --label before|after [--database acme_lab_1m]
"""

import argparse
import statistics
import time

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import Employee
from app.services import directory as d
from scale_lab import lab

TERMS = [
    ("2 characters", "an"),
    ("3 characters, common", "son"),
    ("surname", "abbott"),
    ("part of a full name", "joan abb"),
    ("part of a code", "emp-0065"),
    ("part of an email", "abbott.659"),
    ("no match", "zzzqqq"),
]
RUNS = 5


def request_ms(engine, term: str) -> float:
    started = time.perf_counter()
    with Session(engine) as session:
        d.list_employees(session, d.DirectoryQuery(q=term))
        session.commit()
    return (time.perf_counter() - started) * 1000


def measure_terms(engine) -> dict:
    out = {}
    for label, term in TERMS:
        request_ms(engine, term)  # warm-up
        samples = [request_ms(engine, term) for _ in range(RUNS)]
        with Session(engine) as session:
            matches = session.scalar(
                select(func.count()).select_from(
                    d.apply_filters(
                        select(Employee.id), d.DirectoryQuery(q=term)
                    ).subquery()
                )
            )
        out[label] = {
            "term": term,
            "matches": matches,
            "median_ms": round(statistics.median(samples), 1),
        }
        lab.log(
            f"{label:22} {term!r:14} {matches:>8,} matches  request {out[label]['median_ms']:>8} ms"
        )
    return out


INSERT_SQL = """
INSERT INTO employees (company_id, first_name, last_name, email, department_id,
                       job_title_id, job_level_id, current_country, currency, status, hire_date)
SELECT company_id, first_name, last_name, 'insert-test-' || id || '@x.example',
       department_id, job_title_id, job_level_id, current_country, currency, status, hire_date
FROM employees WHERE status = 'active' ORDER BY id LIMIT 100000
"""


def measure_insert(database: str) -> dict:
    """Seconds to insert 100,000 employees, rolled back; best of three."""
    samples = []
    with lab.connect(database) as conn:
        conn.autocommit = False
        for _ in range(3):
            started = time.perf_counter()
            conn.execute(INSERT_SQL)
            samples.append(time.perf_counter() - started)
            conn.rollback()
    lab.log(f"insert of 100,000 employees: {[round(s, 1) for s in samples]} s")
    return {
        "seconds": [round(s, 2) for s in samples],
        "best_seconds": round(min(samples), 2),
    }


def trigram_index_sizes(database: str) -> dict:
    with lab.connect(database) as conn:
        rows = conn.execute(
            "SELECT indexname, pg_relation_size(indexname::regclass) FROM pg_indexes "
            "WHERE tablename = 'employees' AND indexdef ILIKE '%gin_trgm_ops%'"
        ).fetchall()
    return {name: size for name, size in rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_lab_1m")
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    engine = create_engine(
        lab.lab_url(args.database),
        future=True,
        connect_args={"prepare_threshold": None},
    )
    sizes = trigram_index_sizes(args.database)
    lab.log(f"trigram indexes present: {sizes or 'none'}")
    lab.save(
        f"e13_trigram_{args.label}",
        {
            "database": args.database,
            "trigram_index_bytes": sizes,
            "terms": measure_terms(engine),
            "insert_100k": measure_insert(args.database),
        },
    )


if __name__ == "__main__":
    main()
