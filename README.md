# ACME Salary Management

HR tool to find any employee, see their full compensation history, record changes without ever overwriting the past, and answer pay questions across countries and currencies.

> **Live app:**  <https://acme-salary-management-ee70.onrender.com/> · **Demo video:** <https://www.loom.com/share/254f5132ef4848a1b8cb41a2f25f27e5>

## What's here

| | |
| --- | --- |
| `backend/` | FastAPI + SQLAlchemy + PostgreSQL. Compensation history is append-only, enforced by the database. |
| `frontend/` | React + TypeScript + Vite. See `frontend/README.md`. |
| `docs/` | `REQUIREMENTS.md` (one page), `SPECIFICATION.md`, `DATABASE_DESIGN.md` (with `database-design.mmd` and `database_design.png`), `IMPLEMENTATION_PLAN.md`, `PERFORMANCE.md`, `SCALE_LAB.md`, `AI_USAGE.md` |
| `Dockerfile`, `render.yaml`, `deploy/` | The hosted instance (below) |

## Run it locally

```bash
docker compose up -d db                      # Postgres (fill backend/.env from .env.example first)
cd backend
pip install -r requirements.txt
python -m app.bootstrap                      # migrate + seed 10,000 employees (idempotent)
uvicorn app.main:app --port 8000
# in another terminal
cd frontend && npm install && npm run dev    # http://localhost:5173
```

Tests: `cd backend && pytest` (uses its own `<db>_test` database and never touches the network) and `cd frontend && npm test`.

### Test results

| Suite | Tests | Result | Time | Coverage |
| --- | --- | --- | --- | --- |
| Backend (pytest) | 579 | all passed | ~25–38 s | 93% of `app/` (`pytest --cov=app`) |
| Frontend (vitest) | 88 (11 files) | all passed | ~3 s | not measured |

Run on 2026-10-15 (coverage figure from 2026-10-10). The backend suite includes unit tests for the pure domain layer and integration tests against a real Postgres, so the database must be running (`docker compose up -d db`).

## Environments

Defaults are tuned for the 10,000 employees the app is specified for, and `ENVIRONMENT` defaults to `production`. Features built for a million employees (Scale Lab, `docs/PERFORMANCE.md`) turn on only with `ENVIRONMENT=development`, which `backend/.env` sets for local work.

| | Production / default (10,000 employees) | Development (`ENVIRONMENT=development`) |
| --- | --- | --- |
| Pay insights read pay totals | computed live: always current | from the `employee_totals` materialized view: faster at 1M, up to a few seconds behind a change |
| Background refresh of that view | none | after any change, at the end of the daily job, at bootstrap |
| Postgres in Docker (`docker-compose.yml`) | n/a (Render's managed Postgres) | 512 MB shared memory, 1 GB `shared_buffers`, `pg_stat_statements` |

Not environment-specific, because they cost nothing at 10,000 and help at any size: keyset pagination, the denormalised current-pay table, the pay-insight query rewrites, the 5-minute response cache, trigram search indexes (skipped if the database doesn't allow the extension), disabling prepared statements, and the stored per-day pay-change events (the report would otherwise take seconds on the free tier even at 10,000 employees).

## Deploy

One container serves the built frontend and the API (the API under `/api`), so there is nothing to wire together and no CORS.

**On Render (free tier):** New → Blueprint → pick this repo. `render.yaml` creates the web service from the `Dockerfile` and a managed Postgres, and sets `DATABASE_URL` from it.

**Anywhere else:** build the `Dockerfile` and run it with `DATABASE_URL` pointing at a Postgres 16 database (`postgres://…` URLs are accepted). Optional: `EXCHANGE_RATE_API_URL` (defaults to open.er-api.com), `PORT`, `ANALYTICS_CACHE_SECONDS` (default 300; `0` turns the pay-insight cache off), `COMPRESS_RESPONSES` (default on: gzip for responses of 1 KB or more), `HEAVY_CONCURRENCY` (default 2; the most pay-insight requests that run at once, the rest wait up to `HEAVY_WAIT_SECONDS`, default 60, then get a 503; `0` removes the cap), `PAY_CHANGE_EVENTS` (default on; `false` computes the pay-changes report from the whole history on every request instead of reading stored per-day events), `DB_PING_IDLE_SECONDS` (default 10; ping a pooled connection only after it has been idle that long; `0` pings on every checkout), `REFERENCE_CACHE_SECONDS` (default 60; how long exchange rates are remembered; `0` always reads them), `PROMOTE_ONCE_PER_DAY` (default on), `ANALYTICS_TOTALS_VIEW` (unset: on when `ENVIRONMENT=development`, off otherwise; `true`/`false` forces it), `TOTALS_REFRESH_DELAY` (seconds to wait after a change before refreshing the view, default 2), `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`.

On every start the container runs `python -m app.bootstrap`, then the daily job in the background, then the server:

1. `alembic upgrade head`
2. reference data, compensation catalog, change reasons, org structure, companies (insert-only, safe to repeat)
   (`employee_totals`, a materialized view of every employee's annual total, is kept up to date only in development; see Environments)
3. the first live exchange-rate fetch (a provider outage is logged, not fatal; the daily job retries)
4. 10,000 synthetic employees, **only if there are none**: history is append-only, so it is never reseeded
5. the stored pay-change events (what the pay-changes report reads), computed once from the history when none are stored yet; the daily job then appends each new day

The first start takes about a minute (the seed itself ~10 s on a laptop, the pay-change events another few seconds there); later starts take seconds. Health check: `GET /health`.

The daily job (00:10 UTC) refreshes exchange rates and makes future-dated pay changes current. It runs inside the container; if the host spins a free instance down overnight it catches up on the next start, and the app is correct meanwhile because current pay is also resolved on read.

**Extension:** the directory's search uses `pg_trgm` (indexes in migration `0014`); it is a standard Postgres extension, available on Render's managed Postgres, and the migration creates it (the database user needs permission to).

**Optional hardening:** create a role that can only read and insert compensation records (`APP_DB_PASSWORD=… python -m app.db.grants acme_app`) and set `RUNTIME_DATABASE_URL` to it. Migrations keep using `DATABASE_URL`. Managed Postgres plans that don't allow creating roles skip this; the database triggers still block changes to history.

There is no login: the app is open to anyone with the URL, which is acceptable for a demo on synthetic data only (see Future Roadmap in `docs/SPECIFICATION.md`).

## Performance on the hosted instance

On Render's free tier the web service and the database share a small slice of CPU, so the heavy Pay insights views run roughly 11 to 14 times slower than on a laptop (about 0.7 to 1.0 s against 60 to 90 ms), and the service is spun down when idle: the first request after a pause waits for it to wake up (**54 s** when measured). Everything else is fast.

Server time on the live app with the 10,000 employees (median of 8 requests, measured on 2026-10-19 before and after the optimisation release, pull request #14; the response cache was bypassed):

| Request | Server time, before → after | Database queries |
| --- | --- | --- |
| Reference lists (departments, countries, …) | 6 → 4 ms | 1 → 1 |
| Directory page (50 employees) | 26 → 20 ms | 6 → 3 |
| Directory search | 503 → 207 ms | 6 → 3 |
| Employee profile | 19 → 11 ms | 6 → 3 |
| Pay insights: summary | 1,129 → 934 ms | 5 → 2 |
| Pay insights: outliers | 2,620 → 986 ms | 5 → 2 |
| Pay insights: pay distribution | 1,433 → 697 ms | 6 → 2 |
| CSV export of all 10,000 employees | 4,779 → 1,908 ms | 15 → 3 |

The directory, profile and reference lists stay in the tens of milliseconds, well inside the 500 ms target. Each Pay insights view takes 0.7 to 1.0 s on a first load of a given filter combination, which is the free-tier CPU scanning every active employee's pay. A time measured from a browser also includes the internet: a trivial request took about 310 ms from the author's location whatever the server did. Full method and results: `docs/PERFORMANCE.md` (E25); `python -m scale_lab.live_check` repeats the measurement.

What keeps the page usable:

- **Server time is shown on the page.** Expand the note at the bottom of Pay insights: for each view it shows the server's time (request received to response sent, from the `Server-Timing` header), how much of that was database time and how many queries ran, the browser's round trip, and whether the answer was cached. The gap between the last two is network and waiting.
- **Answers are cached for 5 minutes.** An identical pay-insight request is served from memory until something is written through the API or the time limit passes. Changes made by the daily job can take up to 5 minutes to show. This is per process, which is enough for one instance; several instances would need a shared cache.
- **Views load as you scroll.** Only the first two load straight away; the rest load when you scroll near them, so the page doesn't ask for everything at once.
- **Fewer queries per request.** The Pay insights make 2 queries instead of 5 or 6, and the directory 3 instead of 6: the pay-insight query rewrites, no ping on a busy connection, remembered currencies and exchange rates, and one look for due pay changes a day.
- **The CSV export is smaller and faster:** 397 KB instead of 1.5 MB, and 1.9 s of server time instead of 4.8 s.
- **Static files are cached for good.** The built JavaScript and CSS are served `immutable`, so a repeat visit downloads nothing. (Render's edge already gzips text responses.)

Not known: whether the trigram search indexes exist on the hosted database (the migration skips them if `pg_trgm` is not permitted). Directory search takes 207 ms there against 25 ms on a laptop; `\d employees` in the database shell shows whether `ix_employees_trgm_*` are present.
