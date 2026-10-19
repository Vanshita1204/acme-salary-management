"""E26: the pay-changes report read from stored events, against the live computation.

Needs a database migrated to 0015 and (for the stored report) events stored: run
`python -m app.bootstrap` against it, or pass --backfill to rebuild them here (derived data;
this deletes and recomputes `pay_change_events` in the lab database).

Measures, in-process with a fresh session per call:
- the report for ranges ending today, live and stored (and checks the two are identical);
- the backfill, the daily fill of one day, and recomputing one employee;
- the size of the stored events and what the insert trigger adds to a bulk insert.

Usage (from backend/):
    python -m scale_lab.e26_pay_change_events --database acme_salary
    python -m scale_lab.e26_pay_change_events --database acme_lab_1m --backfill --live-runs 1
"""

import argparse
import statistics
import time
from datetime import timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services import analytics, pay_changes
from app.services import directory as d
from app.services.compensation import utc_today
from scale_lab import lab

RANGES = {"1 week": 7, "1 month": 30, "1 quarter": 91, "1 year": 365}


def timed(call, runs: int) -> float:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 1)


def report(engine, days: int, *, stored: bool):
    get_settings().pay_change_events = stored
    today = utc_today()
    with Session(engine) as session:
        result = analytics.change_report(
            session, d.DirectoryQuery(), today - timedelta(days=days), today, 25
        )
        session.commit()
        return result


def report_times(engine, live_runs: int, stored_runs: int) -> dict:
    out = {}
    for name, days in RANGES.items():
        live = report(engine, days, stored=False)  # also the answer to compare with
        stored = report(engine, days, stored=True)
        row = {
            "events": live.events,
            "employees_changed": live.employees_changed,
            "identical": live == stored,
            "live_ms": timed(
                lambda days=days: report(engine, days, stored=False), live_runs
            ),
            "stored_ms": timed(
                lambda days=days: report(engine, days, stored=True), stored_runs
            ),
        }
        row["speed_up"] = round(row["live_ms"] / row["stored_ms"], 1)
        out[name] = row
        lab.log(f"report, {name:9} {row}")
    return out


def maintenance_times(engine) -> dict:
    """Each in a transaction that is rolled back, so the lab data is untouched."""
    out = {}
    yesterday = utc_today() - timedelta(days=1)
    with Session(engine) as session:
        before = session.scalar(text("SELECT count(*) FROM pay_change_events"))
        session.execute(
            text("UPDATE pay_change_progress SET filled_through = :d"),
            {"d": yesterday - timedelta(days=1)},
        )
        session.execute(
            text("DELETE FROM pay_change_events WHERE day > :d"),
            {"d": yesterday - timedelta(days=1)},
        )
        started = time.perf_counter()
        stored = pay_changes.fill_through_yesterday(session)
        out["fill_one_day"] = {
            "seconds": round(time.perf_counter() - started, 2),
            "events_stored": stored,
            "table_rows_restored": session.scalar(
                text("SELECT count(*) FROM pay_change_events")
            )
            == before,
        }
        session.rollback()
    with Session(engine) as session:
        employee = session.scalar(
            text("SELECT employee_id FROM compensation_records LIMIT 1")
        )
        session.execute(
            text("INSERT INTO pay_change_dirty VALUES (:e, '2012-01-01')"),
            {"e": employee},
        )
        started = time.perf_counter()
        pay_changes.process_dirty(session)
        out["recompute_one_employee_ms"] = round(
            (time.perf_counter() - started) * 1000, 1
        )
        session.rollback()
    lab.log(f"maintenance {out}")
    return out


def size(engine) -> dict:
    with engine.connect() as conn:
        rows, total = conn.execute(
            text(
                "SELECT count(*), pg_total_relation_size('pay_change_events') FROM pay_change_events"
            )
        ).one()
        table = conn.execute(
            text("SELECT pg_size_pretty(pg_relation_size('pay_change_events'))")
        ).scalar()
        indexes = conn.execute(
            text("SELECT pg_size_pretty(pg_indexes_size('pay_change_events'))")
        ).scalar()
    out = {
        "rows": rows,
        "total_mb": round(total / 2**20, 1),
        "table": table,
        "indexes": indexes,
    }
    lab.log(f"size {out}")
    return out


def trigger_overhead(engine, rows: int) -> dict:
    """Copy `rows` existing records (backdated dates already in the stored range, so every
    employee is listed as dirty) with the trigger enabled and then disabled; rolled back."""
    copy = f"""
        INSERT INTO compensation_records (employee_id, compensation_type_id, effective_date, country,
                                          currency, amount, change_reason_id, changed_by)
        SELECT employee_id, compensation_type_id, effective_date, country, currency, amount,
               change_reason_id, 'e26'
          FROM compensation_records ORDER BY id LIMIT {rows}"""
    out = {}
    for label, enabled in (("with_trigger", True), ("without_trigger", False)):
        with Session(engine) as session:
            if not enabled:
                session.execute(
                    text(
                        "ALTER TABLE compensation_records DISABLE TRIGGER trg_comp_records_pay_change_dirty"
                    )
                )
            started = time.perf_counter()
            session.execute(text(copy))
            out[label] = round(time.perf_counter() - started, 2)
            if enabled:
                out["dirty_rows_created"] = session.scalar(
                    text("SELECT count(*) FROM pay_change_dirty")
                )
            session.rollback()
    out["rows_inserted"] = rows
    lab.log(f"trigger overhead {out}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_salary")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="delete and recompute the stored events first",
    )
    parser.add_argument("--live-runs", type=int, default=3)
    parser.add_argument("--stored-runs", type=int, default=5)
    parser.add_argument("--trigger-rows", type=int, default=100_000)
    args = parser.parse_args()
    engine = create_engine(
        lab.lab_url(args.database),
        future=True,
        connect_args={"prepare_threshold": None},
    )
    results: dict = {}
    if args.backfill:
        with Session(engine) as session:
            session.execute(text("DELETE FROM pay_change_dirty"))
            session.execute(text("DELETE FROM pay_change_events"))
            session.execute(
                text("UPDATE pay_change_progress SET filled_through = NULL")
            )
            session.commit()
            started = time.perf_counter()
            stored = pay_changes.backfill(session, commit=True)
            session.commit()
            results["backfill"] = {
                "seconds": round(time.perf_counter() - started, 1),
                "events": stored,
            }
            lab.log(f"backfill {results['backfill']}")
    results["size"] = size(engine)
    results["report"] = report_times(engine, args.live_runs, args.stored_runs)
    results["maintenance"] = maintenance_times(engine)
    results["trigger"] = trigger_overhead(engine, args.trigger_rows)
    get_settings().pay_change_events = True
    lab.save(f"e26_pay_change_events_{args.database}", results)


if __name__ == "__main__":
    main()
