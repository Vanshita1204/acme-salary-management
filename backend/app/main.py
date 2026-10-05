from fastapi import FastAPI

from app.api import (
    change_reasons,
    companies,
    compensation_types,
    employees,
    exchange_rates,
    reference,
)

app = FastAPI(title="ACME Salary Management")
for module in (
    reference,
    companies,
    compensation_types,
    change_reasons,
    employees,
    exchange_rates,
):
    app.include_router(module.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
