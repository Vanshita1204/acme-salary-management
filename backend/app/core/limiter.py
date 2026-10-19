"""A cap on how many heavy requests run at once (Scale Lab E27).

The Pay insights page asks for seven views together. Each is a long scan on the database;
run all at once on a small server they share the CPU and every one finishes late. With a cap,
the rest wait their turn in the API process and each running one finishes sooner. The waiting
requests hold no database connection: the slot is taken before a session is opened.
"""

import threading
from collections.abc import Iterator

from fastapi import HTTPException, status

from app.core.config import get_settings

_slots: threading.BoundedSemaphore | None = None
_slots_size = 0
_lock = threading.Lock()


def _semaphore(size: int) -> threading.BoundedSemaphore:
    """One semaphore per configured size (tests and the lab change the setting)."""
    global _slots, _slots_size
    with _lock:
        if _slots is None or _slots_size != size:
            _slots, _slots_size = threading.BoundedSemaphore(size), size
        return _slots


def limit_heavy_requests() -> Iterator[None]:
    """Dependency: hold one slot while the request runs. A request that has waited longer than
    `heavy_wait_seconds` gets a 503 asking it to retry, rather than queueing without end."""
    settings = get_settings()
    if settings.heavy_concurrency <= 0:
        yield
        return
    slots = _semaphore(settings.heavy_concurrency)
    if not slots.acquire(timeout=settings.heavy_wait_seconds):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The server is busy with other reports. Try again in a moment.",
            headers={"Retry-After": "5"},
        )
    try:
        yield
    finally:
        slots.release()
