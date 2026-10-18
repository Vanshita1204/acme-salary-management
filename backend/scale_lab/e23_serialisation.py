"""E23: how much of a response is serialisation? Times the steps of a directory page and of the
largest pay-insight response separately, plus the framework's fixed cost on a trivial request.

Usage (from backend/):
    python -m scale_lab.e23_serialisation [--database acme_salary]
"""

import argparse
import json
import statistics
import time

import httpx
from pydantic import TypeAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.employees import DirectoryItem, EmployeeOut
from app.schemas.analytics import OutliersOut
from app.schemas.employees import DirectoryPageOut
from app.services import analytics as an
from app.services import directory as d
from scale_lab import lab
from scale_lab.e9_concurrency import BASE, server


def median_ms(call, runs: int = 30) -> float:
    call()
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 3)


def directory_steps(engine) -> dict:
    def service():
        with Session(engine) as session:
            result = d.list_employees(session, d.DirectoryQuery(limit=50))
            session.commit()
            return result

    # The page's rows are ORM objects, so the build step needs a session that is still open.
    with Session(engine) as session:
        page = d.list_employees(session, d.DirectoryQuery(limit=50))

        def build():
            return DirectoryPageOut(
                items=[
                    DirectoryItem(
                        **EmployeeOut.model_validate(row.employee).model_dump(
                            include=set(DirectoryItem.model_fields)
                        ),
                        total_compensation=row.total,
                        total_compensation_reporting=row.total_reporting,
                    )
                    for row in page.rows
                ],
                total=page.total,
                start=page.start,
                next_cursor=page.next_cursor,
                prev_cursor=page.prev_cursor,
                reporting_currency=page.reporting_currency,
                rates_as_of=page.rates_as_of,
            )

        out = build()
        adapter = TypeAdapter(DirectoryPageOut)
        return {
            "service_sql_orm_totals_ms": median_ms(service),
            "build_50_items_ms": median_ms(build),
            "validate_response_model_ms": median_ms(
                lambda: adapter.validate_python(out.model_dump())
            ),
            "dump_json_pydantic_ms": median_ms(lambda: adapter.dump_json(out)),
            "dump_python_then_json_dumps_ms": median_ms(
                lambda: json.dumps(
                    adapter.dump_python(out, mode="json"), separators=(",", ":")
                )
            ),
            "bytes": len(adapter.dump_json(out)),
        }


def outliers_steps(engine) -> dict:
    def service():
        with Session(engine) as session:
            result = an.outliers(session, d.DirectoryQuery())
            session.commit()
            return result

    context, rows = service()
    payload = {
        "reporting_currency": context.reporting_currency,
        "rates_as_of": context.rates_as_of,
        "excluded_no_rate": context.excluded_no_rate,
        "low_threshold": 0.8,
        "high_threshold": 1.2,
        "min_peer_group_size": 5,
        "outliers": rows,
    }
    adapter = TypeAdapter(OutliersOut)
    out = adapter.validate_python(payload, from_attributes=True)
    return {
        "rows": len(rows),
        "service_sql_python_ms": median_ms(service, 10),
        "dump_json_pydantic_ms": median_ms(lambda: adapter.dump_json(out)),
        "dump_python_then_json_dumps_ms": median_ms(
            lambda: json.dumps(
                adapter.dump_python(out, mode="json"), separators=(",", ":")
            )
        ),
        "bytes": len(adapter.dump_json(out)),
    }


def framework_cost(database: str) -> dict:
    """Server time of a request that does almost nothing: routing, a session, one tiny query."""
    with server(database, 1, 5, 10), httpx.Client(base_url=BASE, timeout=60) as client:
        out = {}
        for path in ("/health", "/departments"):
            for _ in range(5):
                client.get(path)
            samples = []
            for _ in range(30):
                header = client.get(path).headers["server-timing"]
                samples.append(float(header.split("app;dur=")[1].split(",")[0]))
            out[path] = round(statistics.median(samples), 2)
        return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_salary")
    args = parser.parse_args()
    engine = create_engine(
        lab.lab_url(args.database),
        future=True,
        connect_args={"prepare_threshold": None},
    )
    results = {
        "directory_page": directory_steps(engine),
        "outliers_view": outliers_steps(engine),
        "framework_server_ms": framework_cost(args.database),
    }
    for name, row in results.items():
        lab.log(f"{name}: {row}")
    lab.save(f"e23_serialisation_{args.database}", results)


if __name__ == "__main__":
    main()
