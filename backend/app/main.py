import time

from fastapi import FastAPI, Request, Response
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
from app.core.cache import CachedResponse, ResponseCache
from app.core.config import get_settings
from app.db.session import QueryStats, query_stats
from app.services.errors import ServiceError

app = FastAPI(title="ACME Salary Management")


response_cache = ResponseCache(get_settings().analytics_cache_seconds)


@app.middleware("http")
async def timing_and_cache(request: Request, call_next):
    """Report how long the server took, from receiving the request to having the response
    ready to send, as a `Server-Timing` header (the browser's own timing also includes the
    network and, on a hosted free tier, any wait for the service to wake up). Pay-insight
    GETs are answered from `response_cache` when an identical one was computed recently;
    any write through the API empties it."""
    started = time.perf_counter()

    def elapsed_ms() -> float:
        return (time.perf_counter() - started) * 1000

    if request.method not in ("GET", "HEAD"):
        response_cache.invalidate()
    cacheable = (
        response_cache.enabled
        and request.method == "GET"
        and "/analytics/" in request.url.path
    )
    key = f"{request.url.path}?{request.url.query}"
    if cacheable and (hit := response_cache.get(key)):
        response = Response(hit.body, hit.status_code, hit.headers)
        response.headers["Server-Timing"] = f'app;dur={elapsed_ms():.1f}, cache;desc="hit"'
        return response

    version = response_cache.version
    stats = QueryStats()
    query_stats.set(stats)
    response = await call_next(request)
    if cacheable and response.status_code == 200:
        body = b"".join([chunk async for chunk in response.body_iterator])
        response_cache.put(
            key, CachedResponse(200, dict(response.headers), body), version
        )
        response = Response(body, response.status_code, dict(response.headers))
    timing = f"app;dur={elapsed_ms():.1f}"
    if stats.count:
        timing += (
            f', db;dur={stats.total_ms:.1f};desc="{stats.count} queries, '
            f'slowest {stats.slowest_ms:.0f} ms"'
        )
    if cacheable:
        timing += ', cache;desc="miss"'
    response.headers["Server-Timing"] = timing
    return response


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
