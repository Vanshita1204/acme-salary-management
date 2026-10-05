#!/bin/sh
# Start of every container: migrate and seed (idempotent), keep the daily job running
# (exchange rates, future-dated pay), then serve. The seed runs before the server
# opens its port, so a first start takes a minute or two and the health check waits.
set -e
python -m app.bootstrap
python -m app.jobs.daily --forever &
exec uvicorn app.web:site --host 0.0.0.0 --port "${PORT:-8000}"
