from fastapi import FastAPI

from app.api import exchange_rates

app = FastAPI(title="ACME Salary Management")
app.include_router(exchange_rates.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
