from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

from app.api import (
    analytics,
    change_reasons,
    companies,
    compensation_types,
    employees,
    exchange_rates,
    imports,
    org,
    reference,
)
from app.services.errors import ServiceError

app = FastAPI(title="ACME Salary Management")


# What people see when something we didn't anticipate goes wrong. Never the exception text:
# that can hold SQL, values or connection details. The server still logs the traceback.
UNEXPECTED_ERROR = "Something went wrong on the server. Nothing you entered was lost: try again, and tell whoever runs this system if it keeps happening."
DATABASE_UNAVAILABLE = "The database isn't reachable right now. Try again in a moment."


@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
def database_unavailable(_: Request, __: Exception) -> JSONResponse:
    """The database is down or the connection was lost (not a problem with the request)."""
    return JSONResponse(status_code=503, content={"detail": DATABASE_UNAVAILABLE})


@app.exception_handler(Exception)
def unexpected_error(_: Request, __: Exception) -> JSONResponse:
    """Any other failure: still JSON in the same shape as every other error."""
    return JSONResponse(status_code=500, content={"detail": UNEXPECTED_ERROR})


@app.exception_handler(ServiceError)
def service_error(_: Request, exc: ServiceError) -> JSONResponse:
    """Business-rule violations from the service layer: 404 / 409 / 422."""
    return JSONResponse(status_code=exc.problem.value, content={"detail": exc.message})


for module in (
    reference,
    companies,
    compensation_types,
    change_reasons,
    employees,
    exchange_rates,
    imports,
    analytics,
):
    app.include_router(module.router)
for router in (org.departments, org.job_titles, org.job_levels):
    app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
