# ACME Salary Management

HR tool to find any employee, see their full compensation history, record changes without ever overwriting the past, and answer pay questions across countries and currencies.

> **Live app:** _add the URL after the first deploy_ · **Demo video:** _add the link_

## What's here

| | |
|---|---|
| `backend/` | FastAPI + SQLAlchemy + PostgreSQL. Compensation history is append-only, enforced by the database. |
| `frontend/` | React + TypeScript + Vite. See `frontend/README.md`. |
| `docs/` | `REQUIREMENTS.md` (one page), `SPECIFICATION.md`, `DATABASE_DESIGN.md`, `IMPLEMENTATION_PLAN.md`, `PERFORMANCE.md`, `SCALE_LAB.md`, `AI_USAGE.md` |
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
|---|---|---|---|---|
| Backend (pytest) | 539 | all passed | ~25–38 s | 93% of `app/` (`pytest --cov=app`) |
| Frontend (vitest) | 81 (10 files) | all passed | ~3 s | not measured |

Run on 2026-10-10. The backend suite includes unit tests for the pure domain layer and integration tests against a real Postgres, so the database must be running (`docker compose up -d db`).

## Deploy

One container serves the built frontend and the API (the API under `/api`), so there is nothing to wire together and no CORS.

**On Render (free tier):** New → Blueprint → pick this repo. `render.yaml` creates the web service from the `Dockerfile` and a managed Postgres, and sets `DATABASE_URL` from it.

**Anywhere else:** build the `Dockerfile` and run it with `DATABASE_URL` pointing at a Postgres 16 database (`postgres://…` URLs are accepted). Optional: `EXCHANGE_RATE_API_URL` (defaults to open.er-api.com), `PORT`.

On every start the container runs `python -m app.bootstrap`, then the daily job in the background, then the server:

1. `alembic upgrade head`
2. reference data, compensation catalog, change reasons, org structure, companies (insert-only, safe to repeat)
3. the first live exchange-rate fetch (a provider outage is logged, not fatal; the daily job retries)
4. 10,000 synthetic employees, **only if there are none**: history is append-only, so it is never reseeded

The first start takes about a minute (the seed itself ~10 s on a laptop); later starts take seconds. Health check: `GET /health`.

The daily job (00:10 UTC) refreshes exchange rates and makes future-dated pay changes current. It runs inside the container; if the host spins a free instance down overnight it catches up on the next start, and the app is correct meanwhile because current pay is also resolved on read.

**Optional hardening:** create a role that can only read and insert compensation records (`APP_DB_PASSWORD=… python -m app.db.grants acme_app`) and set `RUNTIME_DATABASE_URL` to it. Migrations keep using `DATABASE_URL`. Managed Postgres plans that don't allow creating roles skip this; the database triggers still block changes to history.

There is no login: the app is open to anyone with the URL, which is acceptable for a demo on synthetic data only (see Future Roadmap in `docs/SPECIFICATION.md`).
