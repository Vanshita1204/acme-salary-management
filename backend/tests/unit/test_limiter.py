import threading
import time

import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.core.limiter import limit_heavy_requests


@pytest.fixture
def cap():
    settings = get_settings()
    saved = settings.heavy_concurrency, settings.heavy_wait_seconds
    yield settings
    settings.heavy_concurrency, settings.heavy_wait_seconds = saved


def test_a_request_beyond_the_cap_waits_then_gets_a_503(cap):
    cap.heavy_concurrency, cap.heavy_wait_seconds = 1, 0.05
    first = limit_heavy_requests()
    next(first)

    with pytest.raises(HTTPException) as busy:
        next(limit_heavy_requests())
    assert busy.value.status_code == 503
    assert busy.value.headers["Retry-After"]

    first.close()  # releases the slot
    second = limit_heavy_requests()
    next(second)
    second.close()


def test_waiting_requests_run_when_a_slot_frees_up(cap):
    cap.heavy_concurrency, cap.heavy_wait_seconds = 1, 5
    running = peak = 0
    lock = threading.Lock()

    def request():
        nonlocal running, peak
        gate = limit_heavy_requests()
        next(gate)
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1
        gate.close()

    threads = [threading.Thread(target=request) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert peak == 1


def test_zero_turns_the_cap_off(cap):
    cap.heavy_concurrency = 0
    gates = [limit_heavy_requests() for _ in range(5)]
    for gate in gates:
        next(gate)  # none of them blocks
    for gate in gates:
        gate.close()
