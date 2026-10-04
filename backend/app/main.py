from fastapi import FastAPI

app = FastAPI(title="ACME Salary Management")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}