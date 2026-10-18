"""The `employee_totals` materialized view (Scale Lab E12): each employee's annual total
compensation, stored so the pay-insight views don't aggregate all current pay per request.

The view is a snapshot: it is only as fresh as its last refresh. Refreshes happen at
start-up, after the daily job, and shortly after writes made through the API (see
`app.main`). Writes made any other way show up at the next refresh."""

import threading

from sqlalchemy import BigInteger, Column, MetaData, Numeric, Table, text
from sqlalchemy.orm import Session

# Set whenever current pay changed (an API write, or a due future-dated change taking
# effect on a read); the API's refresher thread rebuilds the view shortly after.
stale = threading.Event()

# A separate MetaData: the view is not a table the app creates or migrates through models.
employee_totals = Table(
    "employee_totals",
    MetaData(),
    Column("employee_id", BigInteger, primary_key=True),
    Column("total", Numeric),
)


def refresh_totals(session: Session) -> None:
    """Rebuild the view without blocking readers, then commit."""
    session.execute(text("SELECT refresh_employee_totals()"))
    session.commit()
