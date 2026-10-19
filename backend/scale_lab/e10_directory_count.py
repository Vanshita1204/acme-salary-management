"""E10: what the directory's counts cost at 1M employees, and cheaper alternatives.

Times each part of a directory request (page query, exact total, start-of-page count,
totals, promotion), then the exact count against a capped count, the planner's estimate
and the table-statistics estimate, with accuracy against the exact count.

Usage (from backend/):
    python -m scale_lab.e10_directory_count [--database acme_lab_1m]
"""

import argparse
import statistics
import time

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models import Department, Employee
from app.services import directory as d
from app.services.compensation import promote_due_records
from scale_lab import lab

CAP = 10_001


def median_ms(call, runs: int = 5) -> float:
    call()  # warm-up
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 2)


def planner_estimate(session: Session, query: d.DirectoryQuery) -> int:
    stmt = d.apply_filters(select(Employee.id), query)
    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    plan = session.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")).scalar()
    return int(plan[0]["Plan"]["Plan Rows"])


def capped_count(session: Session, query: d.DirectoryQuery) -> int:
    inner = d.apply_filters(select(Employee.id), query).limit(CAP).subquery()
    return session.scalar(select(func.count()).select_from(inner)) or 0


def stats_estimate(session: Session) -> int:
    return int(
        session.scalar(
            text("SELECT reltuples::bigint FROM pg_class WHERE relname = 'employees'")
        )
    )


def measure_shape(session: Session, query: d.DirectoryQuery) -> dict:
    """Every cost for one request shape, and the alternatives against the exact count."""
    stmt, keys, _ = d.sort_key(query)
    filtered = d.apply_filters(stmt, query)
    # The first row of a page 300,000 rows in, to time the start-of-page count for a deep page.
    row = session.execute(
        filtered.add_columns(*[k.label(f"k{i}") for i, k in enumerate(keys)])
        .order_by(*[k.asc() for k in keys])
        .offset(300_000)
        .limit(1)
    ).first()
    deep_first = tuple(row[i + 1] for i in range(len(keys))) if row else None
    exact = session.scalar(
        select(func.count()).select_from(
            d.apply_filters(select(Employee.id), query).subquery()
        )
    )
    page_query = (
        d.apply_filters(select(Employee), query)
        .order_by(
            func.lower(Employee.last_name), func.lower(Employee.first_name), Employee.id
        )
        .limit(51)
    )
    capped = capped_count(session, query)
    estimate = planner_estimate(session, query)
    return {
        "matches": exact,
        "whole_request_ms": median_ms(lambda: d.list_employees(session, query)),
        "exact_total_ms": median_ms(lambda: d.count_matching(session, query)),
        "page_query_ms": median_ms(lambda: session.execute(page_query).all()),
        "promote_ms": median_ms(lambda: promote_due_records(session)),
        "count_before_deep_ms": (
            median_ms(
                lambda: d.count_before(session, query, filtered, keys, deep_first)
            )
            if deep_first
            else None
        ),
        "capped": {
            "ms": median_ms(lambda: capped_count(session, query)),
            "value": capped,
            "shown": f"{CAP - 1:,}+" if capped >= CAP else f"{capped:,}",
        },
        "planner_estimate": {
            "ms": median_ms(lambda: planner_estimate(session, query)),
            "value": estimate,
            "ratio_to_exact": round(estimate / exact, 2) if exact else None,
        },
    }


def run(database: str) -> dict:
    engine = create_engine(lab.lab_url(database), future=True)
    session = Session(engine)
    engineering = session.scalar(
        select(Department.id).where(Department.name == "Engineering")
    )
    shapes = {
        "no filter": d.DirectoryQuery(),
        "status = active": d.DirectoryQuery(statuses=["active"]),
        "department": d.DirectoryQuery(department_ids=[engineering]),
        "department + country": d.DirectoryQuery(
            department_ids=[engineering], countries=["IN"]
        ),
        "search 'an' (common)": d.DirectoryQuery(q="an"),
        "search 'zzzzq' (no match)": d.DirectoryQuery(q="zzzzq"),
    }
    out: dict = {"requests": {}, "alternatives": {}}
    for name, query in shapes.items():
        entry = measure_shape(session, query)
        session.rollback()
        out["requests"][name] = entry
        lab.log(
            f"{name:28} matches {entry['matches']:>9,}  request {entry['whole_request_ms']:>7} ms  "
            f"exact count {entry['exact_total_ms']:>7} ms  capped {entry['capped']['ms']:>6} ms  "
            f"estimate {entry['planner_estimate']['ms']:>5} ms (x{entry['planner_estimate']['ratio_to_exact']})  "
            f"count_before(deep) {entry['count_before_deep_ms']}"
        )
    out["alternatives"]["table_statistics_estimate"] = {
        "ms": median_ms(lambda: stats_estimate(session)),
        "value": stats_estimate(session),
    }
    lab.log(
        f"unfiltered via pg_class: {out['alternatives']['table_statistics_estimate']}"
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_lab_1m")
    args = parser.parse_args()
    lab.save(f"e10_directory_count_{args.database}", run(args.database))


if __name__ == "__main__":
    main()
