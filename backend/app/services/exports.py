"""CSV export of the employee directory (FR-6)."""

import csv
import io

from app.services.directory import DirectoryExport

EXPORT_COLUMNS = (
    "code",
    "first_name",
    "last_name",
    "email",
    "department",
    "job_title",
    "job_level",
    "country",
    "status",
    "hire_date",
    "termination_date",
    "currency",
    "total_compensation",  # annual CTC, employee's currency
    "reporting_currency",
    "total_compensation_reporting",  # annual CTC, reporting currency
    # Date of the older of the two rates used; empty when nothing was converted.
    "rates_as_of",
)

# A cell starting with one of these is run as a formula by Excel and Sheets
# ("CSV injection"); names and emails come from user input.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: str) -> str:
    return "'" + value if value.startswith(FORMULA_PREFIXES) else value


def directory_csv(export: DirectoryExport) -> str:
    """UTF-8 with a byte-order mark, so Excel shows accented names correctly."""
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out)
    writer.writerow(EXPORT_COLUMNS)
    reporting = export.reporting_currency
    for row in export.rows:
        e = row.employee
        rate_date = None  # no conversion: same currency, no total, or no rate
        if row.total_reporting is not None and e.currency != reporting:
            rate_date = min(export.rate_dates[e.currency], export.rate_dates[reporting])
        writer.writerow(
            [
                e.code,
                safe_text(e.first_name),
                safe_text(e.last_name),
                safe_text(e.email),
                safe_text(e.department),
                safe_text(e.job_title),
                safe_text(e.job_level),
                e.current_country,
                e.status,
                e.hire_date.isoformat(),
                e.termination_date.isoformat() if e.termination_date else "",
                e.currency,
                "" if row.total is None else str(row.total),
                reporting,
                "" if row.total_reporting is None else str(row.total_reporting),
                rate_date.isoformat() if rate_date else "",
            ]
        )
    return out.getvalue()
