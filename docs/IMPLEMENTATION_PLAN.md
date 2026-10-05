# Implementation Plan — ACME Salary Management

Build order for turning the scaffold (FastAPI app, Postgres via docker-compose, Alembic, pytest — all currently empty) into the system described in `docs/REQUIREMENTS.md`, on the schema detailed in `docs/DATABASE_DESIGN.md`. Phases are sequential where a later one depends on an earlier one's output; within a phase, tasks can interleave.

## Guiding principles (from §7 and AI_USAGE.md)
- Domain logic (compensation math, currency conversion, import validation, analytics formulas) is written as pure functions first, unit-tested with no DB or network, before any endpoint touches them.
- Compensation history is append-only from the first migration — no update/delete path on `compensation_records` exists anywhere in the code.
- Business rules (base-pay amount > 0, unique email/code, valid effective date, change reason must exist) are database constraints, not just Pydantic validation.
- The seed script and Scale Lab (Phase 1 experiments E1–E3) run **before** the directory/analytics endpoints are finalized, so pagination and the current-compensation table are validated with real volume instead of assumed.

---

## Phase 0 — Environment: Docker, settings, DB wiring
Everything Phase 1 needs before it can touch a table. Done.
- `docker-compose.yml`: Postgres 16 service, with a `pg_isready` healthcheck so startup can be polled instead of guessed at. Credentials (`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`) come from `backend/.env` via `env_file` — nothing hardcoded. Postgres only applies them when the data volume is first created, so changing the password later also needs `ALTER ROLE` (or a fresh volume).
- `app/core/config.py`: a `Settings` (pydantic-settings) object reading `DATABASE_URL`, `EXCHANGE_RATE_API_URL` and `ENVIRONMENT` from `.env` (`.env.example` committed with keys only, `.env` gitignored). `DATABASE_URL` and `EXCHANGE_RATE_API_URL` are required — no in-code defaults, so a missing value fails at startup instead of silently pointing somewhere. Extra keys in `.env` (the `POSTGRES_*` ones docker-compose uses) are ignored.
- `app/db/session.py`: SQLAlchemy `engine`, `SessionLocal`, declarative `Base`, and a `get_db()` FastAPI dependency.
- `app/models/__init__.py`: empty package Phase 1 fills in, one module per table — imported from `migrations/env.py` so `Base.metadata` is complete for `alembic upgrade`/`--autogenerate` without Phase 1 having to touch env.py again.
- `migrations/env.py`: rewired to pull the DB URL from `Settings` (`alembic.ini` no longer contains a URL at all), and to use `Base.metadata` as `target_metadata`. Calls `fileConfig(..., disable_existing_loggers=False)` so running migrations in-process (the test fixture does) doesn't silence app loggers.
- **Done when (verified):** `docker compose up -d` brings up a healthy Postgres container; the app's `Settings`/`engine` connect to it (`SELECT 1` succeeds); `alembic upgrade head` runs cleanly against it (currently a no-op — zero migrations until Phase 1); `pytest` passes; `TestClient(app).get("/health")` returns `200`.

## Phase 1 — Foundation: models, reference data, basic CRUD — Done
Three steps, in order — nothing in Phase 2 onward starts until all three are done.

### 1.1 Models & migrations — Done
- SQLAlchemy models for every table in `docs/DATABASE_DESIGN.md`: `Currency`, `Country`, `Company`, `CompensationType`, `ChangeReason`, `Employee`, `CompensationRecord`, `CurrentCompensation`, `ExchangeRate`.
- Alembic migrations in dependency order: `currencies` → `countries` → `companies` → `compensation_types` → `change_reasons` → `employees` → `compensation_records` → `current_compensation` → `exchange_rates`.
- Constraints backing §6: base-pay amount `> 0` / other types `>= 0` (trigger, keyed off `is_base_pay`), `period_months > 0`, unique `employees.email`/`.code`, the FK from `change_reason_id` to the shared `change_reasons` list, the `employees.currency`-must-match-new-record-currency trigger, the append-only trigger on `compensation_records`, and the "exactly one base-pay type" partial unique index.
- `employees.code` generation strategy: a `employee_code_seq` sequence behind a server default (`'EMP-' || lpad(nextval(...), 6, '0')`), never client-supplied.
- **As built:** `migrations/versions/0001`–`0009`, one revision per table in the order above. `0006` also installs the `citext` extension; `0007` creates both `compensation_records` triggers as raw SQL (models can't express them). `0005` creates `change_reasons` as one shared list with no link to compensation types. `0009` stores `exchange_rates.rate_to_usd` as `NUMERIC(24,16)` — at 8 decimals, weak currencies lose most of their significant digits (IRR would be off by 0.7%). Both were corrected in place rather than with follow-up migrations, since no database outside development had been migrated yet; any dev database from before needs a fresh `upgrade head`. Revision IDs must stay ≤ 32 characters (`alembic_version.version_num` is `VARCHAR(32)`).
- **Done when (verified):** `alembic upgrade head` creates the full schema against the docker-compose Postgres, `alembic check` reports no drift between models and schema, and `downgrade base` → `upgrade head` round-trips cleanly. Every constraint in `docs/DATABASE_DESIGN.md`'s business-rules table has an integration test in `tests/integration/test_schema_constraints.py` that violates it and asserts the specific constraint/trigger rejected it. Tests run inside a rolled-back transaction and use ISO's reserved test codes (`XTS`, `XA`, …) so they never collide with real reference data.

### 1.2 Reference-data pre-population — Done
Small, structural lookup data the app can't function without — not the 10,000-employee synthetic seed (that's Phase 3). Loaded by an idempotent script rather than a migration data step, so migrations stay schema-only: `python -m app.seed.reference` (run after `alembic upgrade head`; only inserts what's missing, safe to re-run).
- `currencies` and `countries` (with `default_currency`): the **full** ISO 3166-1 / ISO 4217 lists, not a hand-picked subset — 248 countries, 149 currencies (exactly the set that is some country's legal tender). Sourced at seed time from third-party packages, not hand-maintained (`app/seed/iso_data.py`): pycountry for ISO codes and currency names, babel (Unicode CLDR) for English country names and each country's current legal-tender currency — ISO doesn't publish a country → currency mapping. Upgrading those packages is how the data gets updated. `CURRENCY_OVERRIDES` patches places where CLDR lags (currently Bulgaria → EUR, euro since 2026-01-01). Countries with no legal tender (Antarctica) are omitted.
- `companies`: 100 synthetic companies generated with Faker (`python -m app.seed.companies [--count N] [--seed S]`, default 100 / seed 1204). Deterministic for a given seed, count and pinned Faker version, so re-running inserts nothing; covered by `tests/integration/test_companies_seed.py`. Separate command from `app.seed.reference` for now.
- `compensation_types`: the starting universal catalog in `app/seed/compensation_types.py` (`python -m app.seed.compensation_types`; edit `CATALOG` and re-run to extend) — 13 types:
  - fixed: Base Pay (`is_base_pay = true`, monthly)
  - variable: Annual Variable Pay Target (12)
  - bonus: Quarterly Bonus (3), Annual Bonus (12), Retention Bonus (12)
  - equity: Annual Equity Grant RSU (12)
  - allowance: Housing, Transport, Meal (all monthly)
  - reimbursement: Phone & Internet (1, outside CTC), Phone & Internet (CTC) (1, counts toward total), Learning & Development (12, outside CTC), Wellness (12, outside CTC). Every other seeded type counts toward total. The recorded amount is the employee's entitlement/cap for the period, not individual expense claims (those belong to an expenses system, out of scope).
  - Seeded types are only a starting point — HR adds more from the UI (1.3 + Phase 13). Covered by `tests/integration/test_compensation_types_seed.py`.
- `change_reasons`: one shared list, **not tied to compensation types** — any record of any type can use any reason (`app/seed/change_reasons.py`, `python -m app.seed.change_reasons`). 11 reasons: new hire, annual revision, promotion, market adjustment, role change, relocation, bonus payout, equity grant, retention award, policy change, correction. The smoke check requires `new_hire`, `relocation` and `correction`, which business rules look up by code. Covered by `tests/integration/test_change_reasons_seed.py`.
- `exchange_rates`: **live rates, not fixtures** — `app.seed.reference` finishes by calling Phase 8's refresh (below) against the real provider. A provider outage doesn't fail the reference load; it just warns.
- **Seeding order:** `alembic upgrade head`, then `python -m app.seed.reference` (currencies, countries, live rates), `python -m app.seed.companies`, `python -m app.seed.compensation_types`, `python -m app.seed.change_reasons`. Each is idempotent.
- **Done when (verified):** after 1.1's migrations and the loads above, every lookup table is populated and the smoke checks pass — every country's `default_currency` resolves (`check_reference_data`), exactly one base-pay type exists (`check_compensation_types`), and the reasons the rules rely on exist (`check_change_reasons`). Each loader is also tested to insert nothing on a second run.

### 1.3 Basic CRUD — Done
Minimal, business-rule-light REST endpoints over the models above — enough to exercise the schema end-to-end before building the real FR-driven endpoints (Phases 5–12).
- Simple create/list/get for `companies`.
- Compensation-type catalog management — **exposed to the Phase 13 HR UI** (HR adds types like a new bonus or reimbursement without a code change):
  - `GET /compensation-types`, `GET /compensation-types/{id}`.
  - `POST /compensation-types`: creates a type. `is_base_pay` is not accepted — the one base-pay type is seeded, never created from the UI. Validation: `name` and `category` required; `period_months` > 0; `(category, subtype)` unique → `409` with a clear message instead of the raw DB error.
- Change reasons — one shared list, also **exposed to the HR UI**:
  - `GET /change-reasons`.
  - `POST /change-reasons`: add a reason (code + label); duplicate code → `409`. Available to every compensation type immediately.
- No update/delete of types or reasons: existing records point at them, and renaming or removing one would silently rewrite history. Retiring a type (hide from new-record forms, keep for history) is a later addition if needed.
- Simple create/get for `employees` and `compensation_records` (no keyset pagination, filtering, or analytics yet — those are Phase 5+).
- No update/delete endpoints on `compensation_records` — the API surface itself should make append-only obvious, not just the DB trigger.
- **As built:**
  - Routers in `app/api/`, schemas in `app/schemas/`.
  - Read-only `GET /currencies` and `GET /countries` for form dropdowns.
  - `POST/GET /companies`, `GET /companies/{id}`.
  - `POST/GET /compensation-types`, `GET /compensation-types/{id}`. `is_base_pay` in the body is rejected (`extra="forbid"`), so created types are never base pay.
  - `POST/GET /change-reasons`, `GET /change-reasons/{id}`. Codes are normalized to lowercase identifiers.
  - `POST /employees`, `GET /employees/{id}`. `code` is ignored if sent; the database generates it.
  - `POST/GET /employees/{id}/compensation-records` (history newest first), `GET /compensation-records/{id}`. Records take the employee's current country and currency rather than accepting them. `PUT`/`PATCH`/`DELETE` on records return `405`.
  - **Error handling:** rules stay in the database. `app/api/db_errors.py` translates rejections into clean errors: unique violations → `409`; FK/check violations and trigger exceptions → `422`. Messages come from a few templates (`UNIQUE_MESSAGE`, `FOREIGN_KEY_MESSAGE`, `CHECK_MESSAGE`, …), filled in from the model metadata of whichever constraint failed — entity from the table, fields from the constraint's columns, the referenced table for FKs, the `chk_` name for checks; trigger messages pass through as written. Nothing is registered per table or constraint. `Base` uses Postgres's default constraint naming convention so model and database constraint names match. Non-rule database errors (e.g. connection loss) still surface as `500`.
  - **Deferred on purpose:** the initial base-pay record on employee create (Phase 6) and keeping `current_compensation` in step with new records (Phase 7) — 1.3 stays business-rule-light.
- **Done when (verified):** every model can be created and read back through the API against the 1.2 reference data, and every DB rule comes back as a 4xx — `tests/integration/test_crud_api.py`, which loads the 1.2 seeds inside the test transaction. Also checked live: the read endpoints against the seeded dev database.

## Phase 2 — Domain layer (pure functions, no DB/network) — Done
Lives under `backend/app/domain/`, unit-tested under `backend/tests/unit/` (no database, no network, fixed inputs only).
- **Compensation** (`compensation.py`):
  - `annualize(amount, period_months)` = `amount * 12 / period_months`.
  - `total_compensation(components)`: the sum of annualized current components **that count toward total** (`compensation_types.counts_toward_total`). This settles the reimbursement question per type rather than per category, since employers differ on what goes into CTC — see `docs/DATABASE_DESIGN.md`.
  - `percentage_change(previous, new)`: `None` when there's no base.
  - `average_increase(changes)`: excludes corrections (§5) and changes across a currency switch (FR-7: cross-currency increases are deferred), via `counts_as_increase`.
- **Currency** (`currency.py`, done early with Phase 8's rate work): `convert` / `cross_rate` through USD rates (non-USD → non-USD goes via USD, per FR-8), rounded half-even to cents. `rates_to_usd_from_usd_base` inverts the provider's units-per-USD quotes via `str()` so float noise doesn't leak into `Decimal`. Open: zero-decimal currencies (JPY, KRW, …) still round to cents — use ISO 4217 minor units when the UI formats amounts.
- **Analytics** (`analytics.py`) — the reference definitions Phase 12's SQL is tested against:
  - Peer groups: title + level + country.
  - `find_outliers`: below `OUTLIER_LOW` (0.8) / above `OUTLIER_HIGH` (1.2) of the peer median, only for groups ≥ `MIN_PEER_GROUP_SIZE` (5); boundaries aren't outliers.
  - `composition`: shares per open-ended category.
- **CSV import validation** (`csv_import.py`, FR-5):
  - `validate_import(header, records, ImportContext)`. The caller loads companies, countries, currencies and existing emails once, so the module never touches the DB.
  - Template columns are `TEMPLATE_COLUMNS` — first and last name are separate columns, matching the employee model.
  - Checks: missing header columns (row 1), the 10,000-row cap, required fields, email format (`email-validator`, no DNS lookup), email already existing or repeated within the file (pointing at the first row), unknown company (case-insensitive name), unknown country, unsupported currency, ISO dates, base pay > 0 with at most 2 decimals (thousands separators accepted).
  - Every error has row/column/reason; reasons are module constants. The result is all-or-nothing: `ok` only when there are no errors at all.
- **Done when (verified):**
  - `tests/unit/test_domain_purity.py` parses every domain module and fails on any import of SQLAlchemy, psycopg, httpx, FastAPI or the app's DB/model/service layers.
  - Tests use fixed exchange-rate fixtures, never live rates.
  - `annualize` is tested for monthly/quarterly/semi-annual/annual (and 24-month) periods.

## Phase 3 — Seed script (10,000-employee synthetic dataset) — Done
Distinct from Phase 1.2's reference-data pre-population — this generates the large synthetic employee population on top of it.
- **Command:** `python -m app.seed.employees [--count N] [--seed S] [--as-of YYYY-MM-DD] [--batch-size B]` (defaults: 10,000 / 1204 / today / 5,000).
- **Deterministic:** the same count, seed, as-of date and pinned Faker version produce identical data. Seed-only constants (pay levels, the USD rates for USD-paid staff) never read live exchange rates.
- **Fresh databases only:** refuses to run if employees exist. History is append-only and can't be cleared selectively, so the reset is `alembic downgrade base && alembic upgrade head` plus the 1.2 seeds. It fails with a list of what's missing if 1.2 hasn't run.
- **Population:**
  - 16 hiring countries, weighted (India 30%, US 18%, UK 8%, …), each with a Faker locale for names and a typical local L3 salary.
  - Currency is the country's default, except a share of UAE/Singapore/Mexico staff paid in USD.
  - 8 departments with pay multipliers and titles; levels L1–L7.
  - Status ~86% active / 4% on leave / 10% terminated (termination date between hire + 90 days and the as-of date).
  - Hire dates from 2012, skewed toward recent years.
  - Each employee belongs to one of the seeded companies.
  - ~3% are deliberate pay outliers, so Phase 12 has something to find.
- **History:**
  - Base pay: new hire, then 1 April annual revisions (3–9%) or occasional promotions (12–20%), plus rare corrections. The starting salary is solved backwards so today's pay lands near the market level.
  - Bonuses: Sales gets a variable-pay target and quarterly payouts; everyone else from L2 up gets an annual bonus.
  - Equity: L4+ engineering/product staff get RSU hire grants and annual refreshes.
  - Allowances: housing/transport/meal where customary, revised with each review.
  - Reimbursements: internet for everyone (inside or outside CTC per company policy), learning/wellness for some.
  - Nothing is dated after termination or the as-of date. Every row passes the DB triggers (hire date, currency, base pay > 0).
- **Inserts:** `COPY` by default, chosen by Phase 4's E1 (`--method row|orm|batch|copy` keeps the alternatives). The seed streams in 5,000-employee chunks, committing each, so memory stays flat even at 1M. `current_compensation` is filled per chunk in one `DISTINCT ON` statement.
- **Done when (verified):** 10,000 employees, ~180,000 compensation records and ~43,700 current-compensation rows in ~18 s on the docker-compose Postgres. Checks:
  - Every employee has exactly one current base-pay row, and no record falls after a termination date.
  - Local medians are plausible (e.g. India ₹21 L base, US $124 k).
  - `tests/unit/test_employee_seed.py` covers determinism and history invariants without a DB; `tests/integration/test_employee_seed_db.py` runs a 150-employee seed in a rolled-back transaction.
- **Known simplification:** no relocations or currency changes in the generated history — those need FR-7's ordering (update the employee, then write records) and come with Phase 11.

## Phase 4 — Scale Lab, Phase 1 experiments (before submission) — Done
Per `docs/SCALE_LAB.md` E1–E3, run at S (10k) and M (1M); full results in **`docs/PERFORMANCE.md`**.
- **Harness:** `backend/scale_lab/`.
  - Each experiment builds its own `acme_lab_*` database with the app's migrations and seeds, so the dev database is never touched.
  - Timing: warm-up + 5 runs, median reported, `EXPLAIN (ANALYZE, BUFFERS)` captured.
  - Raw results go to `scale_lab/results/*.json`; `python -m scale_lab.report` renders the tables.
  - To make 1M possible, the seed was made streaming (5,000-employee chunks, commit per chunk, flat memory), with the insert method selectable (`app/seed/loaders.py`: row / orm / batch / copy).
- **E1 bulk loading:** `COPY` (20.7k rows/s) > batched `INSERT` (12.5k) > ORM (7.2k) > row-by-row (2.6k). A checksum confirmed identical data across methods. Linear to 1M (1M employees + 17.8M records in ~17 min). → **The seed now defaults to `COPY`.**
  - **Found a real bug at 1M:** `lpad` truncation made hire #1,000,000's code collide with `EMP-100000`. Fixed in migration `0006` (`employee_code()` function) with a boundary test.
- **E2 pagination:** at 1M, `OFFSET` takes 1.6–2.6 s for the last page; keyset stays < 1 ms everywhere. → **Keyset confirmed** (used by Phase 5).
- **E3 current compensation:**
  - Pages are ~2 ms with any strategy, but only with the composite `(employee, type, effective_date)` index; without it, 2.9 s at 1M. → **Index confirmed essential.**
  - For whole-population queries the `current_compensation` table is 3–4× faster than deriving from history. → **Table confirmed.**
  - **Limit:** even with the table, the compensation sort and country aggregates take 2.9–4.2 s at 1M (22–61 ms at 10k). → Before scaling past the deployed 10k, add `employee_totals` with an indexed USD total, and/or materialized analytics (E5).
- **Done when (verified):** the pagination and current-compensation design used in Phase 5+ rests on these measurements (`docs/PERFORMANCE.md` summary table).

## Phase 5 — Employee Directory API (FR-1) — Done
- **`GET /employees`:**
  - **Search:** `q` matches full name, last name, email or employee code, case-insensitive, with LIKE wildcards escaped.
  - **Filters:** `department`, `country`, `job_title`, `job_level`, `status`. They combine with AND; repeat one for OR within it (`?country=IN&country=US`).
  - **Sort:** `name` (default), `hire_date` or `compensation`, with `order=asc|desc`. Ties always break on `id`.
  - **Paging:** `limit` 1–200 (default 50). `reporting_currency` defaults to USD.
- **Keyset pagination** (confirmed by Scale Lab E2 — `OFFSET` takes 1.6–2.6 s for the last page at 1M employees, keyset stays under 1 ms):
  - Responses carry opaque `next_cursor` / `prev_cursor` (`app/domain/pagination.py`). Each cursor encodes its sort, order, direction and the boundary row's key.
  - Previous pages are read by walking the index backwards and reversing the result.
  - A cursor from a different sort or order, or one that's been tampered with, is a `400`, never a `500`.
- **Totals** come from `current_compensation` (E3), annualized and limited to `counts_toward_total` types. Each item has `total_compensation` (employee's currency) and `total_compensation_reporting` (converted via `app.domain.currency`; `null` if a rate is missing). `rates_as_of` lists the date of every rate used.
- **Sorting by compensation** orders by the total in USD at the latest stored rates, computed in SQL. That is the same order for any reporting currency, since conversion divides every total by one rate. It can't use an index — it aggregates every matching employee's current compensation — which is the trade-off recorded in `docs/DATABASE_DESIGN.md`. See Phase 4's 1M result for when it matters.
- Logic in `app/services/directory.py`; tests in `tests/integration/test_directory_api.py` (every sort paged forward then back across all pages, cross-currency ordering, filters, search, cursor errors) and `tests/unit/test_pagination.py`.
- **Done when (verified):** `python -m scale_lab.directory_latency` — 15 search/filter/sort combinations, first and second pages, 300 requests through the full FastAPI stack on the 10k seed: **p95 42 ms** (target < 500 ms). Slowest are the compensation sort (~42 ms median) and searches that scan without an index (~28 ms). At 1M (informational) index-backed requests stay under ~65 ms, but those two reach 2.7–3.6 s and 0.8–1.3 s — the scaling path is `employee_totals` and a `pg_trgm` search index (Scale Lab E4); see `docs/PERFORMANCE.md`.

## Phase 6 — Employee Profile, Create/Edit/Terminate (FR-2, FR-3)
- `GET /employees/{id}`: full detail + current breakdown per type (local + reporting currency, annualized) + reverse-chronological history per type with reason, note, changed by, and % change (Phase 2 formula).
- `POST /employees`: requires an initial compensation record for the base-pay type (`is_base_pay = true`) with reason "new hire" (atomic — both rows or neither). Other types are added afterward via Phase 7.
- `PATCH /employees/{id}`: employee fields only; no compensation fields accepted here.
- `POST /employees/{id}/terminate`: sets status *and* `termination_date` together (the DB `CHECK` requires both), never deletes.
- **Done when:** editing an employee cannot change compensation, creating one cannot skip the base-pay record, and terminating without a reachable `termination_date` is rejected — enforced in the service layer and covered by tests.

## Phase 7 — Compensation Change (FR-4)
- `POST /employees/{id}/compensation`: appends a record for one `compensation_type_id` with any change reason from the shared list (incl. "correction"), updates `CurrentCompensation` for that type if the new record's effective date is today-or-past and newer than the current pointer for that type.
- Future-dated records: stored but don't move the current pointer until their date arrives — needs a resolution strategy (recompute-on-read for "is this now current" vs. a scheduled job); pick the simplest that matches §7's no-background-jobs-at-10k stance (recompute-on-read).
- Validation: effective date ≥ hire date (§6); change reason must exist (enforced by the FK, but surface a clean 4xx before hitting the DB error).
- **Done when:** a future-dated raise doesn't affect current analytics until its date, a correction doesn't alter average-increase calculations (§5 definitions), and submitting a nonexistent reason is rejected with a clear error.

## Phase 8 — Exchange Rates (FR-8)
**Partly done early** (pulled forward so 1.2 could load live rates instead of fixtures):
- Provider: open.er-api.com (ExchangeRate-API's free, keyless `latest/USD` feed, updated once a day). URL comes from `EXCHANGE_RATE_API_URL`. It quotes every seeded currency except KPW; converting KPW returns a clear 503.
- `app/services/exchange_rates.py`: fetch → parse → upsert one row per supported currency per `rate_date` (re-fetching the same day updates rather than duplicates); `latest_rates` reads the newest rate per currency (`DISTINCT ON`) with each rate's date. On provider failure: log a warning, keep serving stored rates.
- **No provider call per page view.** Conversions and dashboards only read stored rates (§7 Resilience). The refresh itself is throttled: if the newest stored rate was fetched within `REFRESH_INTERVAL` (24 h, matching the provider's publish cadence), it's a no-op that returns `skipped: true` without touching the network — so it's safe to trigger on every dashboard load. `force` overrides.
- Endpoints: `GET /exchange-rates/convert?amount=&from=&to=` (converted amount, cross rate, and the date of each rate used), `GET /exchange-rates/latest`, `POST /exchange-rates/refresh[?force=true]`. CLI: `python -m app.services.exchange_rates [--force]`.
- Tests fake the provider with `httpx.MockTransport` and use far-future (2099) rate dates so they never depend on, or collide with, real stored rates; they cover failure fallback, same-day upsert, throttling (provider not called while fresh, called once stale, called when forced).

**Remaining:**
- Daily scheduling. Because refresh is throttled, the simplest option is to trigger it on app startup and/or from the dashboard's first load rather than a separate scheduler; a cron calling the CLI also works. Pick one and document it.
- Every analytics/profile response that converts currency surfaces the rate date used (the convert endpoint already does).
- Reporting-currency selector; conversion always routes through stored USD rates.
- **Done when:** analytics still return correct numbers with the fetch disabled, using the previous day's stored rates — tested by simulating provider failure (already tested at the service level; still needed for the analytics endpoints).

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
- **Compensation types screen** (backed by 1.3's catalog endpoints): table of every type — name, category, subtype, payment period — plus an **"Add compensation type"** form:
  - Name, category and subtype as free text, with category suggesting existing values (e.g. `bonus`, `reimbursement`) to keep grouping consistent in the Phase 12 composition breakdown.
  - Payment period as a picker (Monthly = 1, Quarterly = 3, Semi-annual = 6, Annual = 12) with a "custom months" escape hatch, stored as `period_months`.
  - "Counts toward total compensation (CTC)" toggle, on by default — e.g. off for an internet reimbursement the employer pays outside CTC.
  - No base-pay checkbox (there's exactly one, seeded).
  - Duplicate category/subtype and other validation errors shown inline; the new type appears immediately in the compensation-change form's type dropdown.
- **Change reasons screen:** the shared list, with an "Add reason" form (code + label). The compensation-change form (Phase 7) offers every reason for every type, with "Correction" always available.
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
- Hosted instance with managed Postgres, seeded on first run (per §9): `alembic upgrade head`, then the 1.2 seed commands (currencies/countries + first live rate fetch, companies, compensation catalog), then the Phase 3 seed. Both are idempotent.
- README with live URL and demo video at the top (per AI_USAGE.md's deployment decision).

## Phase 16 — Scale Lab, Phase 2 experiments (post-submission, optional)
Per `docs/SCALE_LAB.md` E4–E8 (trigram search, materialized analytics, large-import `COPY` staging, index write cost, partitioning) — informs the §8 1M-path roadmap but isn't required for the 10k submission.

---

## Cross-cutting, continuous
- **Testing split (§7):** unit tests on Phase 2's pure functions (`tests/unit/`, fast, no infra); integration tests for API/DB behavior against the docker-compose Postgres (`tests/integration/`). The integration fixture runs `alembic upgrade head` once per session and wraps every test in a rolled-back transaction, so the dev DB is never polluted. Tests never call the live exchange-rate provider. They currently share the dev database (`acme_salary`); split off a dedicated test database once Phase 3's seed data lives there.
- **AI_USAGE.md:** log Phase 3 entries as they happen (per the file's existing template) — what was generated, what was changed, what bugs were caught by tests.
- **Sequencing risk:** Phases 5 and 12 both depend on Phase 4's conclusions about the current-compensation strategy — don't hand-build either against an unvalidated assumption.
