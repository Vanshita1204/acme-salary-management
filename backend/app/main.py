from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import (
    change_reasons,
    companies,
    compensation_types,
    employees,
    exchange_rates,
    org,
    reference,
)
from app.services.errors import ServiceError

app = FastAPI(title="ACME Salary Management")


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
):
    app.include_router(module.router)
for router in (org.departments, org.job_titles, org.job_levels):
    app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
