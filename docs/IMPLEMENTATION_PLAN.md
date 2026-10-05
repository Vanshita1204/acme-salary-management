# Implementation Plan — ACME Salary Management

Build order for turning the scaffold (FastAPI app, Postgres via docker-compose, Alembic, pytest — all currently empty) into the system described in `docs/REQUIREMENTS.md`, on the schema detailed in `docs/DATABASE_DESIGN.md`. Phases are sequential where a later one depends on an earlier one's output; within a phase, tasks can interleave.

## Guiding principles (from §7 and AI_USAGE.md)
- Domain logic (compensation math, currency conversion, import validation, analytics formulas) is written as pure functions first, unit-tested with no DB or network, before any endpoint touches them.
- Compensation history is append-only from the first migration — no update/delete path on `compensation_records` exists anywhere in the code.
- Business rules (base-pay amount > 0, unique email/code, valid effective date, change reason valid for its type) are database constraints, not just Pydantic validation.
- The seed script and Scale Lab (Phase 1 experiments E1–E3) run **before** the directory/analytics endpoints are finalized, so pagination and the current-compensation table are validated with real volume instead of assumed.

---

## Phase 0 — Environment: Docker, settings, DB wiring
Everything Phase 1 needs before it can touch a table. Done.
- `docker-compose.yml`: Postgres 16 service, with a `pg_isready` healthcheck so startup can be polled instead of guessed at.
- `app/core/config.py`: a `Settings` (pydantic-settings) object reading `DATABASE_URL`/`ENVIRONMENT` from `.env` (`.env.example` committed, `.env` gitignored) — one source of truth for the connection string.
- `app/db/session.py`: SQLAlchemy `engine`, `SessionLocal`, declarative `Base`, and a `get_db()` FastAPI dependency.
- `app/models/__init__.py`: empty package Phase 1 fills in, one module per table — imported from `migrations/env.py` so `Base.metadata` is complete for `alembic upgrade`/`--autogenerate` without Phase 1 having to touch env.py again.
- `migrations/env.py`: rewired to pull the DB URL from `Settings` instead of the hardcoded value in `alembic.ini` (which stays only as a fallback), and to use `Base.metadata` as `target_metadata`.
- **Done when (verified):** `docker compose up -d` brings up a healthy Postgres container; the app's `Settings`/`engine` connect to it (`SELECT 1` succeeds); `alembic upgrade head` runs cleanly against it (currently a no-op — zero migrations until Phase 1); `pytest` passes; `TestClient(app).get("/health")` returns `200`.

## Phase 1 — Foundation: models, reference data, basic CRUD
Three steps, in order — nothing in Phase 2 onward starts until all three are done.

### 1.1 Models & migrations
- SQLAlchemy models for every table in `docs/DATABASE_DESIGN.md`: `Currency`, `Country`, `Company`, `CompensationType`, `ChangeReason`, `Employee`, `CompensationRecord`, `CurrentCompensation`, `ExchangeRate`.
- Alembic migrations in dependency order: `currencies` → `countries` → `companies` → `compensation_types` → `change_reasons` → `employees` → `compensation_records` → `current_compensation` → `exchange_rates`.
- Constraints backing §6: base-pay amount `> 0` / other types `>= 0` (trigger, keyed off `is_base_pay`), `period_months > 0`, unique `employees.email`/`.code`, the composite FK tying `change_reason_id` to its `compensation_type_id`, the `employees.currency`-must-match-new-record-currency trigger, the append-only trigger on `compensation_records`, and the "exactly one base-pay type" partial unique index.
- `employees.code` generation strategy (e.g. sequence-backed, not client-supplied).
- **Done when:** `alembic upgrade head` creates the full schema against the docker-compose Postgres; every constraint in `docs/DATABASE_DESIGN.md`'s business-rules table has a corresponding integration test that violates it and gets rejected.

### 1.2 Reference-data pre-population
Small, structural lookup data the app can't function without — not the 10,000-employee synthetic seed (that's Phase 3).
- `currencies` and `countries` (with `default_currency`): a fixed, hand-maintained list (ISO 4217 / ISO 3166-1), loaded by a migration data-seed step or a small idempotent script.
- At least one `companies` row (ACME itself, or its known entities).
- `compensation_types` + `change_reasons`: the starting universal catalog — base pay (`is_base_pay = true`, `period_months = 1`) plus a handful of representative variable/equity/allowance/bonus types, each with its seeded set of valid reasons (every type gets "correction").
- `exchange_rates`: an initial fetch-or-seed against the chosen provider (or a fixed fixture set if the provider isn't wired up yet) so `employees.currency` and reporting-currency conversion have something to read from day one.
- **Done when:** a fresh database, after 1.1's migrations and this step's data load, has every lookup table populated and passes a smoke check (e.g. every seeded country's `default_currency` resolves, every compensation type has a "correction" reason).

### 1.3 Basic CRUD
Minimal, business-rule-light REST endpoints over the models above — enough to exercise the schema end-to-end before building the real FR-driven endpoints (Phases 5–12).
- Simple create/list/get for `companies`, `compensation_types`, `change_reasons` (admin-style management of the catalog, not exposed to the Phase 13 HR-facing UI).
- Simple create/get for `employees` and `compensation_records` (no keyset pagination, filtering, or analytics yet — those are Phase 5+).
- No update/delete endpoints on `compensation_records` — the API surface itself should make append-only obvious, not just the DB trigger.
- **Done when:** every model can be created and read back through the API against the pre-populated reference data from 1.2, confirming the schema works end-to-end before any domain logic or business workflow is layered on.

## Phase 2 — Domain layer (pure functions, no DB/network)
Lives under `backend/app/domain/`, unit-tested under `backend/tests/unit/`.
- Compensation: `annualize(amount, period_months)` (= `amount * 12 / period_months`), total-compensation calc (sum of annualized current types), percentage change between two records of the same type (correction-aware per §5 definitions).
- Currency: conversion through stored USD rates (non-USD → non-USD goes via USD, per FR-8).
- Analytics formulas: peer-group grouping (title, level, country), outlier threshold (80%/120% of peer median, peer groups ≥ 5), composition breakdown grouped by `compensation_types.category`.
- CSV import validation rules (FR-5): required fields, email format/uniqueness-within-file, recognized company, supported currency, non-negative amounts, base-pay amount > 0, date validity — returns row/column/reason, doesn't touch the DB.
- **Done when:** these modules have no `import` of SQLAlchemy session or an HTTP client; tests run with fixed exchange-rate fixtures, never live rates (§7 Testability); `annualize` is tested against monthly/quarterly/semi-annual/annual inputs.

## Phase 3 — Seed script (10,000-employee synthetic dataset)
Distinct from Phase 1.2's reference-data pre-population — this generates the large synthetic employee population on top of it.
- Deterministic (`--seed` flag, default fixed), `--count` flag for Scale Lab reuse.
- Assigns each employee a company (from Phase 1.2's seeded rows), a country-appropriate currency, and role/level-based amounts for a representative subset of the compensation-type catalog; generates realistic compensation history per employee (hire + a few revisions per type), consistent with the domain layer from Phase 2.
- Batched inserts first; revisit with `COPY` based on Phase 4's E1 result.
- **Done when:** `python -m seed --count 10000` populates a fresh, already-reference-seeded DB in a bounded time and produces data the directory/analytics endpoints (Phases 5, 8) can be built against.

## Phase 4 — Scale Lab, Phase 1 experiments (before submission)
Per `docs/SCALE_LAB.md` E1–E3, run at S (10k) and at least M (1M):
- E1 bulk loading method → fixes the seed script's insert strategy.
- E2 keyset vs. offset pagination → confirms the FR-1 pagination approach before the directory endpoint is built on it.
- E3 current-compensation strategy (`DISTINCT ON` / window function / denormalized table) → confirms or revises the Phase 1 data model.
- Record results in `docs/PERFORMANCE.md` as each experiment completes; feed conclusions back into Phases 1 and 3 before moving on.
- **Done when:** the pagination and current-compensation design used in Phase 5+ is evidence-based, not assumed.

## Phase 5 — Employee Directory API (FR-1)
- `GET /employees`: keyset pagination (next/previous), free-text search (name/email/code), filters (department, country, title, level, status, combinable), sort (name, hire date, compensation).
- Reads current total compensation from the `CurrentCompensation` table (no per-request recomputation), converted to the selected reporting currency via Phase 2's currency module.
- Indexes on every filtered/sorted column (§7).
- **Done when:** p95 latency on the 10k seed is under 500 ms (§1) for a representative mix of filter/search/sort combinations.

## Phase 6 — Employee Profile, Create/Edit/Terminate (FR-2, FR-3)
- `GET /employees/{id}`: full detail + current breakdown per type (local + reporting currency, annualized) + reverse-chronological history per type with reason, note, changed by, and % change (Phase 2 formula).
- `POST /employees`: requires an initial compensation record for the base-pay type (`is_base_pay = true`) with reason "new hire" (atomic — both rows or neither). Other types are added afterward via Phase 7.
- `PATCH /employees/{id}`: employee fields only; no compensation fields accepted here.
- `POST /employees/{id}/terminate`: sets status *and* `termination_date` together (the DB `CHECK` requires both), never deletes.
- **Done when:** editing an employee cannot change compensation, creating one cannot skip the base-pay record, and terminating without a reachable `termination_date` is rejected — enforced in the service layer and covered by tests.

## Phase 7 — Compensation Change (FR-4)
- `POST /employees/{id}/compensation`: appends a record for one `compensation_type_id` (incl. "correction" reason, which must be validated against that same type), updates `CurrentCompensation` for that type if the new record's effective date is today-or-past and newer than the current pointer for that type.
- Future-dated records: stored but don't move the current pointer until their date arrives — needs a resolution strategy (recompute-on-read for "is this now current" vs. a scheduled job); pick the simplest that matches §7's no-background-jobs-at-10k stance (recompute-on-read).
- Validation: effective date ≥ hire date (§6); change reason valid for the record's compensation type (enforced by the composite FK, but surface a clean 4xx before hitting the DB error).
- **Done when:** a future-dated raise doesn't affect current analytics until its date, a correction doesn't alter average-increase calculations (§5 definitions), and submitting a reason that belongs to a different type is rejected with a clear error.

## Phase 8 — Exchange Rates (FR-8)
- Daily fetch job from an external provider into `ExchangeRate`; on failure, log and keep serving the latest stored rates (§1, §7 Resilience).
- Every analytics/profile response that converts currency surfaces the rate date used.
- Reporting-currency selector; conversion always routes through stored USD rates.
- **Done when:** analytics still return correct numbers with the fetch disabled, using the previous day's stored rates — tested by simulating provider failure.

## Phase 9 — CSV Import (FR-5)
- Template download endpoint. Columns: name, email, company, department, title, level, country, hire date, currency, base pay amount — the base-pay type only; other compensation types are added afterward through Phase 7, not at import.
- `POST /import/validate`: full-file validation via Phase 2's rules, returns per-row errors; nothing persisted.
- `POST /import/confirm`: re-validates and inserts all-or-nothing in one transaction, writing each employee's row plus one base-pay compensation record with reason "new hire" (reject silently-stale previews — re-validate against current DB state, e.g. emails created since the preview).
- 10,000-row cap enforced before validation runs.
- **Done when:** a file with one bad row (of many) results in zero rows saved, with the bad row's number/column/reason reported.

## Phase 10 — CSV Export (FR-6)
- Exports the Phase 5 directory view's current filter/search/sort state, with local + reporting currency columns.
- **Done when:** export output matches what the directory UI is showing when exported.

## Phase 11 — Country and Currency Changes (FR-7)
Two distinct operations, both built here:
- **Country change:** `POST /employees/{id}/relocate` — updates `employees.current_country` and, through Phase 7's compensation-change path, writes one new record for the base-pay type with reason "relocation" (same amount/currency unless currency is also changing). This is what gives a country-only move a dated, reasoned history entry even though nothing numeric changed.
- **Currency change:** `POST /employees/{id}/change-currency` — a dedicated endpoint, *not* Phase 7's single-type path. Takes an HR-provided amount for every one of the employee's current compensation types and, in one transaction, updates `employees.currency` and writes a new record per type (each going through the normal Phase 7 validation, so `employees.currency` must be updated first within the transaction). No automatic conversion (see `docs/DATABASE_DESIGN.md`'s interpretive-calls section for why); the live exchange rate may be shown in the UI as a non-binding starting suggestion only.
- Cross-currency increase/percentage-change display explicitly deferred (§4 FR-7, §10 roadmap item 2) — surface as "not comparable, currency changed" in the UI rather than computing a misleading number.
- **Done when:** a country-only relocation shows up in history without changing any amount; a currency change leaves no compensation type stranded in the old currency (every current type is rewritten in the same transaction); and the history view flags a currency change instead of showing a bogus % change across it.

## Phase 12 — Pay Insights / Analytics (§5)
Each view: active employees only, current compensation, selected reporting currency, respects directory filters, shows rates-as-of date.
- Summary cards + cost breakdown (overall/country/department).
- Grouped statistics (avg/median/min/max by department, country, title, level) via SQL `percentile_cont`.
- Role-by-country comparison.
- Histogram of pay distribution.
- Outlier list (peer-group logic from Phase 2, groups ≥ 5 only).
- Change report with date range + average increase (corrections excluded).
- Composition breakdown, grouped by `compensation_types.category` (open-ended, not a fixed base/variable split — see `docs/DATABASE_DESIGN.md`).
- **Done when:** every row in the §5 table maps to a working view, computed SQL-side (not pulled into Python), on the 10k seed.

## Phase 13 — Frontend (React/Vite)
- Directory table with search/filter/sort/pagination (Phase 5).
- Employee profile + history (Phase 6), compensation-change form (Phase 7), relocation flow and the currency-change screen — one editable amount field per current compensation type, submitted together (Phase 11).
- Import wizard: upload → validation report → confirm (Phase 9); export button (Phase 10).
- Analytics dashboard matching the §5 views (Phase 12), with reporting-currency selector and rate-date display (Phase 8).
- Validation messages, currency formatting, and confirmation prompts before any destructive-feeling action (§7 Usability).
- **Done when:** every §1 success criterion is reachable from the UI without exporting to Excel.

## Phase 14 — Non-functional hardening
- Re-run the 500 ms directory check (§1, §7) against the final schema/indexes with the full 10k seed and realistic filter combinations.
- Confirm append-only is structurally impossible to violate (no UPDATE/DELETE grants or code paths on `compensation_records`).
- Error handling and validation messages reviewed end-to-end (API error shapes → frontend display).

## Phase 15 — Deployment
- Hosted instance with managed Postgres, seeded on first run (per §9).
- README with live URL and demo video at the top (per AI_USAGE.md's deployment decision).

## Phase 16 — Scale Lab, Phase 2 experiments (post-submission, optional)
Per `docs/SCALE_LAB.md` E4–E8 (trigram search, materialized analytics, large-import `COPY` staging, index write cost, partitioning) — informs the §8 1M-path roadmap but isn't required for the 10k submission.

---

## Cross-cutting, continuous
- **Testing split (§7):** unit tests on Phase 2's pure functions (fast, no infra); integration tests for API/DB behavior against the docker-compose Postgres (constraints, pagination, transactions).
- **AI_USAGE.md:** log Phase 3 entries as they happen (per the file's existing template) — what was generated, what was changed, what bugs were caught by tests.
- **Sequencing risk:** Phases 5 and 12 both depend on Phase 4's conclusions about the current-compensation strategy — don't hand-build either against an unvalidated assumption.
