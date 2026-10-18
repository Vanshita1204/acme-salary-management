"""E24: database round trips per request, and what network latency does to them.

A small TCP proxy sits between the API and Postgres. It counts round trips (a message from
the client after a reply from the server) and delays every message by half of a chosen
round-trip time in each direction, so a database on another machine can be simulated on
one. The API runs twice, with the round-trip savings off (the behaviour before E24) and on
(the defaults), and each request is timed at several latencies.

Usage (from backend/):
    python -m scale_lab.e24_round_trips [--database acme_salary] [--latencies 0,1,3]
"""

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import threading
import time

import httpx
from sqlalchemy.engine import make_url

from scale_lab import lab

PROXY_PORT = 5433
API_PORT = 8906
BASELINE = {
    "DB_PING_IDLE_SECONDS": "0",
    "REFERENCE_CACHE_SECONDS": "0",
    "PROMOTE_ONCE_PER_DAY": "false",
}


class LatencyProxy:
    def __init__(self, listen_port: int, target_port: int = 5432) -> None:
        self.listen_port, self.target_port = listen_port, target_port
        self.one_way = 0.0  # seconds added to every message, each way
        self.round_trips = 0
        self._last = "s"
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(
            asyncio.start_server(self._handle, "127.0.0.1", self.listen_port)
        )
        self._ready.set()
        self._loop.run_until_complete(server.serve_forever())

    async def _handle(self, client_reader, client_writer) -> None:
        server_reader, server_writer = await asyncio.open_connection(
            "127.0.0.1", self.target_port
        )
        await asyncio.gather(
            self._pump(client_reader, server_writer, "c"),
            self._pump(server_reader, client_writer, "s"),
        )

    async def _pump(self, reader, writer, side: str) -> None:
        queue: asyncio.Queue = asyncio.Queue()

        async def deliver() -> None:
            while True:
                ready, data = await queue.get()
                if data is None:
                    break
                wait = ready - self._loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                writer.write(data)
                await writer.drain()

        task = asyncio.create_task(deliver())
        try:
            while data := await reader.read(65536):
                if side == "c" and self._last != "c":
                    self.round_trips += 1
                self._last = side
                queue.put_nowait((self._loop.time() + self.one_way, data))
        finally:
            queue.put_nowait((0, None))
            await task
            writer.close()


def start_api(database: str, extra_env: dict[str, str]) -> subprocess.Popen:
    url = (
        make_url(lab.lab_url(database))
        .set(port=PROXY_PORT)
        .render_as_string(hide_password=False)
    )
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "RUNTIME_DATABASE_URL": "",
        "ENVIRONMENT": "production",
        "ANALYTICS_CACHE_SECONDS": "0",
        "COMPRESS_RESPONSES": "false",
        **extra_env,
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(API_PORT),
            "--log-level",
            "warning",
        ],
        cwd=lab.BACKEND,
        env=env,
    )
    for _ in range(120):
        try:
            if (
                httpx.get(f"http://127.0.0.1:{API_PORT}/health", timeout=2).status_code
                == 200
            ):
                return process
        except httpx.HTTPError:
            time.sleep(0.5)
    raise RuntimeError("the API did not start")


def measure(proxy: LatencyProxy, latencies: list[float]) -> dict:
    client = httpx.Client(base_url=f"http://127.0.0.1:{API_PORT}", timeout=60)
    first_id = client.get("/employees", params={"limit": 1}).json()["items"][0]["id"]
    endpoints = {
        "reference list (/departments)": "/departments",
        "directory page": "/employees?limit=50",
        "employee profile": f"/employees/{first_id}",
        "pay insight: summary": "/analytics/summary",
    }
    out: dict = {}
    for rtt in latencies:
        proxy.one_way = rtt / 2000
        for name, path in endpoints.items():
            for _ in range(4):
                client.get(path)
            trips, times = [], []
            for _ in range(15):
                before = proxy.round_trips
                started = time.perf_counter()
                client.get(path)
                times.append((time.perf_counter() - started) * 1000)
                trips.append(proxy.round_trips - before)
            out.setdefault(name, {})[str(rtt)] = {
                "round_trips": statistics.median(trips),
                "median_ms": round(statistics.median(times), 1),
            }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="acme_salary")
    parser.add_argument(
        "--latencies", default="0,1,3", help="added round-trip time in ms"
    )
    args = parser.parse_args()
    latencies = [float(n) for n in args.latencies.split(",")]
    proxy = LatencyProxy(PROXY_PORT)
    results: dict = {}
    for label, env in (("before (savings off)", BASELINE), ("after (defaults)", {})):
        process = start_api(args.database, env)
        try:
            results[label] = measure(proxy, latencies)
        finally:
            process.terminate()
            process.wait(timeout=30)
        for name, by_latency in results[label].items():
            lab.log(
                f"{label:20} {name:30} "
                + "  ".join(
                    f"{rtt} ms: {v['round_trips']:g} trips, {v['median_ms']} ms"
                    for rtt, v in by_latency.items()
                )
            )
    lab.save(f"e24_round_trips_{args.database}", results)


if __name__ == "__main__":
    main()
