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
- `app/core/config.py`: a `Settings` (pydantic-settings) object reading `DATABASE_URL`, `EXCHANGE_RATE_API_URL` and `ENVIRONMENT` from `.env` (`.env.example` committed with keys only, `.env` gitignored). `DATABASE_URL` is required — no in-code default, so a missing value fails at startup instead of silently pointing somewhere (Phase 15 gave `EXCHANGE_RATE_API_URL` a default, the one provider the app is built for, and made `postgres://` URLs from hosts work). Extra keys in `.env` (the `POSTGRES_*` ones docker-compose uses) are ignored.
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
- **Seeding order:** `alembic upgrade head`, then `python -m app.seed.reference` (currencies, countries, live rates), `python -m app.seed.companies`, `python -m app.seed.compensation_types`, `python -m app.seed.change_reasons`, `python -m app.seed.org_structure` (Phase 7A). Each is idempotent.
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

## Phase 6 — Employee Profile, Create/Edit/Terminate (FR-2, FR-3) — Done
- **`GET /employees/{id}?reporting_currency=`** — the profile:
  - the employee
  - the current breakdown per type: amount per period, annualized, annualized in the reporting currency, whether it counts toward CTC
  - total CTC in local and reporting currency, and `rates_as_of`
  - the full history newest first. Each record has its type, reason (code + label), note, changed by, the previous same-type amount, `percent_change`, and `currency_changed`. When the currency differs from the previous record, the % change is `null` and flagged rather than computed across currencies (FR-7).
- **`POST /employees`** requires `base_pay` (amount > 0) and `changed_by`. In one transaction it writes the employee, a "new hire" base-pay record effective on the hire date, and the current-compensation row.
  - `status` may only be `active` / `on_leave`; termination fields and `code` are rejected (`extra="forbid"`).
  - Atomicity is enforced by `translate_db_errors`, which now rolls back on *any* failure, not only DB errors. A test simulates the record write failing and checks no employee row survives; it fails without the rollback.
- **`PATCH /employees/{id}`** edits details only: company, names, email, department, title, level, and `active`↔`on_leave`.
  - Compensation, currency/country (FR-7, Phase 11), hire date, termination fields and `code` are rejected with `422`; tests confirm current pay and history are unchanged afterwards.
  - Terminated employees are read-only (`409`).
- **`POST /employees/{id}/terminate`** `{termination_date}` sets status and date together and never deletes. Rejected when:
  - the date is missing
  - it's before the hire date or in the future
  - the employee has compensation records effective after it
  - the employee is already terminated (`409`)

  Terminated employees stay searchable in the directory.
- **Current compensation:** `app/services/compensation.py` `append_record` is now the single write path for records, also used by `POST /employees/{id}/compensation-records`. A record moves the `(employee, type)` pointer only if it's in effect today and newer than the current one — a back-dated correction doesn't displace a newer record; a future-dated record is stored but not yet current (resolved on read since Phase 7).
- Business-rule errors are `ServiceError(Problem.NOT_FOUND|CONFLICT|INVALID, message)`, with message constants in `app/services/employees.py`, mapped to 404/409/422 by one exception handler in `app/main.py`.
- **Done when (verified):** `tests/integration/test_employee_lifecycle_api.py` (32 tests) — editing can't change compensation, creating can't skip base pay, and termination without a reachable date is rejected; plus profile totals, % change, and current-pointer behavior. The Phase 1.3 CRUD tests were updated to the new create contract. Checked live on a seeded employee's profile.

## Phase 7 — Compensation Change (FR-4) — Done
- `POST /employees/{id}/compensation`: appends a record for one `compensation_type_id` with any change reason from the shared list (incl. "correction"), updates `CurrentCompensation` for that type if the new record's effective date is today-or-past and newer than the current pointer for that type.
- Future-dated records: stored but don't move the current pointer until their date arrives — needs a resolution strategy (recompute-on-read for "is this now current" vs. a scheduled job); pick the simplest that matches §7's no-background-jobs-at-10k stance (recompute-on-read).
- Validation: effective date ≥ hire date (§6); change reason must exist (enforced by the FK, but surface a clean 4xx before hitting the DB error).
- **As built:**
  - `app/services/compensation.py` `record_change` validates, then goes through `append_record` (still the single write path). Body: `compensation_type_id`, `change_reason_id`, `effective_date`, `amount`, `changed_by`, optional `note`; country/currency are rejected (`extra="forbid"`) — records always take the employee's current ones. The response is the record plus `is_current` (`false` while future-dated, or for a back-dated correction when a newer record is current).
  - Checked before the database, with the same wording the DB layer uses so a rule reads the same wherever it's caught: employee exists (`404`); compensation type and change reason exist; effective date ≥ hire date; base pay > 0 (other types ≥ 0, by schema); for a terminated employee, effective date ≤ termination date (a correction from while they were employed is still allowed). All `422`, nothing written.
  - The Phase 1.3 `POST /employees/{id}/compensation-records` path is kept (hidden from the schema) and runs the same handler.
  - **Future-dated records — recompute-on-read:** `promote_due_records` runs at the start of every current-compensation read (directory, profile; Phases 10/12 must call it too). One upsert moves each (employee, type) pointer to its newest record that is now due and newer than what's current. Candidates come from a new partial index (`0010_future_dated_records`: records whose `effective_date` is after their UTC creation date), and already-current or superseded ones are filtered out before the upsert, so when nothing is due it's a no-op read — ~2 ms median on the 10k seed. The GET endpoints commit after building their response, so a promotion is persisted once rather than recomputed on every read.
- **Done when (verified):** `tests/integration/test_compensation_change_api.py` (23 tests): a future-dated raise leaves profile and directory totals unchanged the day before and moves them on its date (clock injected); several pending records promote the newest due one and leave later ones pending; promotion never displaces a newer current record; a back-dated correction is stored but not current; corrections drop out of `average_increase`; an unknown reason/type, pre-hire date, zero base pay, or post-termination date is a clear `422` that writes nothing. `alembic check` clean, `downgrade base`/`upgrade head` round-trips, `EXPLAIN` shows the partial index used. Checked live on the seeded 10k database.

## Phase 7A — Departments, Job Titles and Levels as reference data — Done
Found during Phase 7: `employees.department`, `.job_title` and `.job_level` are free text. Nothing stops "Software Engineer", "software engineer" and "SW Engineer" from being three different roles, which is the exact problem REQUIREMENTS §1 lists ("inconsistent department or title names go uncaught"). It also splits every grouping that keys off them: grouped statistics, role-by-country, and the title + level + country peer groups behind outlier detection. Levels sort as text, so `L10` would land between `L1` and `L2`. Inserted as 7A, not renumbered, because later phase numbers are referenced throughout code and docs; it must land before Phase 9 (import validates against these lists) and Phase 12 (groups by them).

- **Tables** (migration `0011`), lookup data like `compensation_types` and `change_reasons`:
  - `departments (id, name CITEXT UNIQUE, created_at)`.
  - `job_titles (id, name CITEXT UNIQUE, created_at)` — **universal, not tied to a department**, like the compensation-type catalog: "Analyst" is a real title in Finance and in Operations, and a department link would force duplicates or block moves. Revisit if HR wants titles scoped per department.
  - `job_levels (id, code CITEXT UNIQUE, label, rank INT UNIQUE, created_at)` — `rank` gives levels a real order for sorting, filters like "L4 and above", and analytics; `code` is what HR sees (`L1`…`L7`).
  - `CITEXT` + `UNIQUE` makes case-variant duplicates impossible at the DB, matching how `employees.email` works; names are trimmed in the API before insert.
- **Employees:** `department_id`, `job_title_id`, `job_level_id` — `NOT NULL` FKs replacing the three text columns, each indexed (replacing `ix_employees_department/_title/_level`).
  - Migration backfills from existing data: insert the distinct trimmed values into each lookup table, set the FKs by case-insensitive match, then drop the text columns. Existing levels get `rank` from their numeric suffix. Downgrade restores the text columns from the joins. Dev databases keep their data — no reseed needed.
- **API** (`app/api/`, same contract as 1.3's catalog endpoints, exposed to the Phase 13 HR UI):
  - `GET/POST /departments`, `GET/POST /job-titles`, `GET/POST /job-levels` plus `GET /…/{id}`. Duplicate name/code/rank → `409` via `db_errors`.
  - No delete: employees and history point at them. Renaming is safe (it's a label, nothing numeric changes), so `PATCH` for name/label is allowed; retiring an entry (hidden from new-employee forms, kept for existing employees) is a later addition if needed.
- **Employee create/edit (Phase 6):** take `department_id`, `job_title_id`, `job_level_id`; unknown id → `422` checked before the DB (same pattern as Phase 7's validation). Responses (`EmployeeOut`, profile, directory items) return both the id and the name/code, so clients don't need a second lookup.
- **Directory (Phase 5):**
  - Filters take ids (`?department_id=3&department_id=5`, repeatable for OR); `job_level` additionally accepts `min_level_rank`/`max_level_rank`.
  - Search (`q`) is unchanged (name/email/code). Re-run `scale_lab.directory_latency` on the 10k seed — the extra joins are to three tiny tables, so p95 should stay well under the 500 ms target; record the number.
- **Seeds:**
  - `python -m app.seed.org_structure` loads the departments, titles and levels the employee seed uses (moved out of `app/seed/employees.py` constants into one catalog module), idempotent like the other 1.2 loaders, with a smoke check that every seeded level has a unique rank.
  - `app.seed.employees` looks ids up by name and writes the FK columns (COPY path included); fails with a clear message if `org_structure` hasn't run.
- **Downstream phases, updated to match:**
  - Phase 9 (CSV import): the template keeps human-readable department/title/level columns; `ImportContext` gets the known names, and an unknown one is a row error ("unknown job title") — the import never creates new reference rows implicitly. Matching is case-insensitive and trims whitespace.
  - Phase 12 (analytics): group by ids, label by name, order levels by `rank`; peer groups become `(job_title_id, job_level_id, country)` — `app/domain/analytics.py` keeps working on plain values, the SQL just feeds it ids.
  - Phase 13 (frontend): dropdowns instead of free-text inputs, plus small "Add department / title / level" screens alongside the compensation-type and change-reason ones.
- **Out of scope, noted:** title/level changes aren't dated — a promotion overwrites `employees.job_level`, so "what level was this employee at in 2023" can't be answered, and the change report can't attribute a raise to a promotion except via the `promotion` change reason. A dated `employee_role_history` (or role columns on compensation records) is a candidate for a later phase; this phase only fixes consistency.
- **As built** (differences from the plan above in bold):
  - Models in `app/models/org.py`; `Employee` loads its three lookups eagerly and exposes `department`, `job_title` (names) and `job_level` (code) as read-only properties, so `EmployeeOut`, profile and directory items return each **id and its name** — the response field names `department`/`job_title`/`job_level` are unchanged, now as display values.
  - Migration `0011_org_structure`: ranks are assigned by the code's number, then alphabetically (`L2` before `L10`; codes without a number last); labels follow the seed's rule (`L3` → `Level 3`). Case/whitespace variants merge into one row.
  - Routers in `app/api/org.py`: `GET/POST /departments`, `/job-titles`, `/job-levels`, `GET /…/{id}`, and `PATCH /…/{id}` (rename; levels can also be re-labelled and re-ranked). `DELETE` is `405`. Names are trimmed.
  - `app/services/employees.py` `check_org_references` rejects unknown ids on create and edit before the DB, with the same wording as the FK message (`department does not exist`, …). The old free-text fields are rejected (`extra="forbid"`).
  - Directory: `department_id`, `job_title_id`, `job_level_id` (repeatable), `min_level_rank`/`max_level_rank`, and **a new `sort=level`** (rank, then name; keyset-paged like the other sorts).
  - Seed: `app/seed/org_structure.py` (`DEPARTMENT_TITLES`, `LEVEL_CODES`) is the source of the names; **`app/seed/employees.py` keeps only the pay model** (`DEPARTMENT_PAY`, `LEVELS` weights/multipliers), with a unit test that both cover exactly the same departments and levels. Same RNG sequence as before, so the seed output is unchanged.
  - `app/domain/csv_import.py` already resolves department/title/level names (case-insensitive) to ids and reports `unknown department` / `unknown job title` / `unknown job level` per row — Phase 9 only has to load the lists into `ImportContext`.
  - Scale Lab: lab databases run `org_structure`; `directory_latency` resolves names to ids and adds two level requests; `e2_pagination` reads the id columns.
- **Verified:**
  - 308 tests pass on both the migrated 10k database and a fresh one. New: `test_org_structure_api.py`, `test_org_structure_seed.py`, constraint tests in `test_schema_constraints.py` (FKs, case-insensitive uniqueness, unique code/rank), directory filter/sort tests, CSV unit tests.
  - Migrated the seeded 10k dev database in place: all 10,000 employees keep identical department/title/level values (compared row by row against a pre-migration export); `downgrade` restores identical text columns; `alembic check` clean. A throwaway database with `" engineering "` / `"Engineering"` / `l2` / `L10` confirmed variants merge and `L10` ranks after `L2`.
  - Fresh database: `app.seed.employees` without `org_structure` fails listing what's missing; with it, the 10k seed is row-for-row identical to the pre-7A seed.
  - Directory latency re-measured: p95 70–73 ms vs 65 ms before 7A on the same machine and data (target < 500 ms) — see `docs/PERFORMANCE.md`.
- **Done when:**
  - Creating or editing an employee with an unknown department/title/level is a `422`; inserting a case-variant duplicate name (`"engineering"` vs `"Engineering"`) is a `409`, tested at both the API and the DB constraint (`tests/integration/test_schema_constraints.py`).
  - The migration upgrades a seeded 10k dev database in place — same employee count, every employee's department/title/level name unchanged — and round-trips through downgrade; `alembic check` clean.
  - Directory filters by id and level-rank range are tested; directory latency on the 10k seed is re-measured and stays under target.
  - Levels sort by rank (`L2` before `L10`) in the directory and the level list.

## Phase 8 — Exchange Rates (FR-8) — Done
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

**As built (remaining items):**
- **Daily scheduling — a worker, `app/jobs/daily.py`:** `python -m app.jobs.daily` runs once (cron: `10 0 * * *`, i.e. 00:10 UTC, shortly after the provider publishes); `--forever` is the long-running worker form (runs at start, then sleeps until the next 00:10 UTC). Each run:
  1. refreshes rates, then commits;
  2. runs Phase 7's `promote_due_records`, then commits — the same daily trigger the future-dated pay switch needs.

  A provider failure is logged, keeps the stored rates, doesn't stop step 2, and exits non-zero so cron / the container restart policy surfaces it.
  - The job doesn't wait out the API's 24 h throttle (a 00:10 run would otherwise skip because yesterday's fetch landed at 00:10:02). `refresh_exchange_rates` takes a `max_age`; the job passes `MIN_REFETCH_INTERVAL` (1 h), which only stops a crash-looping worker from hammering the provider. `POST /exchange-rates/refresh` keeps the 24 h throttle.
  - Reads still promote due records themselves (Phase 7), so a late or missed job never shows stale pay; with the job running, that read-time check finds nothing and stays a no-op. Removing it later is a one-line change per read path.
  - Deployment wiring (cron entry or a worker container) belongs to Phase 15.
- **Rate dates:** the directory and profile responses already return `rates_as_of` for every currency they convert through (FR-8); Phase 12's analytics must do the same.
- **Reporting currency:** `reporting_currency` on the directory and profile is validated against `currencies` — an unknown code is `422 unsupported currency: XYZ` (it used to return empty conversions silently). A supported currency with no stored rate (KPW) still returns `null` conversions rather than failing the page. Conversion always goes through stored USD rates (`app.domain.currency`). The UI's selector lists `GET /exchange-rates/latest`.
- **Done when (verified):** `tests/integration/test_daily_job.py` (12 tests) — with the provider failing (fake transport returning 503), the job reports failure, the profile and directory still convert with the previous stored rates and show those rates' date, and due records are still promoted; a successful fetch is used by the next read; the job doesn't refetch within an hour but does refetch inside the API's 24 h window; worker wake-up times; reporting-currency validation. Also run live: the provider is unreachable from the build sandbox (403), and the job logged the failure, promoted, and exited 1. The analytics endpoints (Phase 12) must get the same provider-failure test when they're built.


## Phase 9 — CSV Import (FR-5) — Done
- Template download endpoint. Columns: name, email, company, department, title, level, country, hire date, currency, base pay amount — the base-pay type only; other compensation types are added afterward through Phase 7, not at import.
- `POST /import/validate`: full-file validation via Phase 2's rules, returns per-row errors; nothing persisted. Department, title and level must match Phase 7A's reference lists (case-insensitive); unknown values are row errors.
- `POST /import/confirm`: re-validates and inserts all-or-nothing in one transaction, writing each employee's row plus one base-pay compensation record with reason "new hire" (reject silently-stale previews — re-validate against current DB state, e.g. emails created since the preview).
- 10,000-row cap enforced before validation runs.
- **Done when:** a file with one bad row (of many) results in zero rows saved, with the bad row's number/column/reason reported.
- **As built:**
  - Endpoints in `app/api/imports.py`, service in `app/services/imports.py`; validation stays the pure `app.domain.csv_import` from Phase 2 (extended in 7A).
    - `GET /import/template`: the header row as a CSV download, nothing else (a sample row could be imported by mistake).
    - `POST /import/validate` (multipart `file`): `{ok, row_count, errors[{row, column, reason}], preview}` — always `200`; `preview` is the first 100 valid rows as they'd be saved (ids for company/department/title/level). Nothing is written.
    - `POST /import/confirm` (multipart `file` + form `changed_by`): `201 {created, employee_ids}`, or `422` with the same report as `/validate` and nothing saved.
  - **Parsing:** UTF-8 (an Excel byte-order mark is accepted; anything else is `422`), header names trimmed and case-insensitive, blank lines skipped, extra columns ignored. Uploads over 10 MB are `413` (the cap only needs ~1 MB for 10,000 template rows).
  - **Row cap:** the 10,000-row check runs before any row is validated; an over-long file gets one error on row 1.
  - **Lookups:** the database context is loaded once per request. Only the file's own emails are checked against `employees`, so the lookup doesn't grow with the employee table.
  - **Confirm re-validates** against the current database — an email created between preview and confirm is reported, not skipped.
  - **Inserts are set-based** in one transaction: employees, then their "new hire" base-pay records (effective on the hire date, the DB triggers still check each), then current-compensation pointers for hire dates up to today. A future hire date is stored like any future-dated record and becomes current on that date (Phase 7/8). Every imported employee starts `active`.
  - **Atomicity:** any failure after the first insert (simulated in a test) rolls the whole file back through `translate_db_errors`.
- **Done when (verified):** `tests/integration/test_import_api.py` (19 tests) — a 50-row file with one bad row (row 38, unknown country) is a `422` reporting that row, column and reason with zero employees saved; validate saves nothing; every bad row in a file is reported; duplicates within the file and existing emails; case-insensitive names and headers; BOM and blank lines; the row cap; non-UTF-8 and oversized files; stale previews; rollback after a mid-insert failure; future hire dates. Timed live on a fresh database: a 10,000-row file validates in 1.1 s and confirms in 4.2 s; re-uploading it is rejected with 10,000 "already exists" errors.


## Phase 10 — CSV Export (FR-6) — Done
- Exports the Phase 5 directory view's current filter/search/sort state, with local + reporting currency columns.
- **Done when:** export output matches what the directory UI is showing when exported.
- **As built:**
  - `GET /employees/export` takes exactly the directory's parameters (search, filters, sort, order, `reporting_currency`) and returns every match as a CSV download (`employees_YYYY-MM-DD.csv`), unpaged.
  - **Same view by construction:** the parameters are one shared FastAPI dependency (`directory_query`), and `app/services/directory.py` `export_employees` runs the directory's own sort-key and filter statement without the page limit. Totals come from the same `with_totals` helper the directory pages use (factored out of `list_employees`). Due future-dated records are promoted first, as on every read.
  - **Columns** (`app/services/exports.py` `EXPORT_COLUMNS`): code, names, email, department, title, level, country, status, hire and termination dates, currency, annual total in the employee's currency, reporting currency, annual total in the reporting currency, and `rates_as_of` — the older of the two rate dates used for that row's conversion (empty when nothing was converted). Plain decimals, ISO dates.
  - **Excel-safe:** UTF-8 with a byte-order mark (accented names display correctly), and text cells starting with `=`, `+`, `-`, `@`, tab or CR are prefixed with `'` so a spreadsheet doesn't run them as formulas (CSV injection).
  - Built in memory: ~1.35 MB for 10,000 employees. Past the deployed size, stream it instead.
- **Done when (verified):** the export tests in `tests/integration/test_directory_api.py` (13 new) page through the directory 2 rows at a time and compare it with the export for six views — every sort, ascending and descending, filters, search, three reporting currencies — same employees, same order, same totals in both currencies. Reversing the export's sort order fails all six. Also tested: columns and values (including a same-currency row with no rate date and a converted row with its rate date), terminated employees, formula neutralising, an empty view, and that invalid parameters are rejected exactly as by the directory. On the 10k seed a full export takes ~0.5 s.

## Phase 11 — Country and Currency Changes (FR-7) — Done
Two distinct operations, both built here:
- **Country change:** `POST /employees/{id}/relocate` — updates `employees.current_country` and, through Phase 7's compensation-change path, writes one new record for the base-pay type with reason "relocation" (same amount/currency unless currency is also changing). This is what gives a country-only move a dated, reasoned history entry even though nothing numeric changed.
- **Currency change:** `POST /employees/{id}/change-currency` — a dedicated endpoint, *not* Phase 7's single-type path. Takes an HR-provided amount for every one of the employee's current compensation types and, in one transaction, updates `employees.currency` and writes a new record per type (each going through the normal Phase 7 validation, so `employees.currency` must be updated first within the transaction). No automatic conversion (see `docs/DATABASE_DESIGN.md`'s interpretive-calls section for why); the live exchange rate may be shown in the UI as a non-binding starting suggestion only.
- Cross-currency increase/percentage-change display explicitly deferred (§4 FR-7, §10 roadmap item 2) — surface as "not comparable, currency changed" in the UI rather than computing a misleading number.
- **Done when:** a country-only relocation shows up in history without changing any amount; a currency change leaves no compensation type stranded in the old currency (every current type is rewritten in the same transaction); and the history view flags a currency change instead of showing a bogus % change across it.
- **As built** (`app/services/relocation.py`, endpoints in `app/api/employees.py`):
  - `POST /employees/{id}/relocate` `{country, effective_date, changed_by, note?}` — updates `current_country` and re-records base pay at its current amount and currency with reason `relocation`. Optional `currency` + `amounts` (must come together) make it a combined move: every current type is re-recorded in the new currency, all as `relocation`, in one transaction.
  - `POST /employees/{id}/change-currency` `{currency, amounts[{compensation_type_id, amount}], change_reason_id, effective_date, changed_by, note?}` — the reason is any from the shared list (e.g. market adjustment); no reason is added for currency changes, since the amounts are HR's, not computed.
  - Both return `{employee, records}`, each record with `is_current`.
  - Every record goes through Phase 7's `record_change` after the employee row is updated and flushed (the DB trigger compares against the employee's currency).
- **Rules that keep current pay in one currency** (what "no type stranded" needed beyond the plan):
  - `amounts` must name **exactly** the employee's current types — a missing, extra or duplicated type is a `422` naming the ids.
  - The effective date must be today or earlier, and not before any current record of the rewritten types. Otherwise a new record wouldn't become current (a future one waits for its date; an older one sits behind the newer record), leaving that type current in the old currency while the employee row has moved. For a country-only move the same rule applies to base pay, so a back-dated move can't re-record today's amount at a date when it was different.
  - A currency change is refused (`409`) while the employee has future-dated records — those would become current later in the old currency.
  - As a last line of defence, the service raises if any rewritten type didn't become current, which rolls the whole transaction back.
  - Terminated employees can't be relocated or change currency (`409`); unknown or unchanged country/currency is `422`; PATCH still rejects both fields.
- **History:** unchanged from Phase 6 — a record whose currency differs from the previous one of its type shows `currency_changed: true` and `percent_change: null` (with the previous amount for reference). A country-only relocation shows `0.00`%.
- **Done when (verified):** `tests/integration/test_relocation_api.py` (22 tests) — a country-only relocation adds one history entry at the same amount and leaves the total unchanged, with earlier records keeping their old country; a currency change rewrites all three current types and a SQL check finds no current row in a currency other than the employee's (also checked after every rejected or failed attempt); the rewritten history entries are flagged rather than given a percent; missing/extra/duplicate types, zero base pay, unknown or same currency, pending future changes, back-dated and future dates, terminated employees; a failure on the second of three records rolls back the currency and every record; the combined relocation + currency change. Removing the "every type" check fails three of these tests.


## Phase 12 — Pay Insights / Analytics (§5) — Done
Each view: active employees only, current compensation, selected reporting currency, respects directory filters, shows rates-as-of date.
- Summary cards + cost breakdown (overall/country/department).
- Grouped statistics (avg/median/min/max by department, country, title, level) via SQL `percentile_cont`. Grouped by Phase 7A's ids, levels ordered by `rank`.
- Role-by-country comparison.
- Histogram of pay distribution.
- Outlier list (peer-group logic from Phase 2, groups ≥ 5 only).
- Change report with date range + average increase (corrections excluded).
- Composition breakdown, grouped by `compensation_types.category` (open-ended, not a fixed base/variable split — see `docs/DATABASE_DESIGN.md`).
- **Done when:** every row in the §5 table maps to a working view, computed SQL-side (not pulled into Python), on the 10k seed.
- **As built** (`app/services/analytics.py`, `app/api/analytics.py`; all `GET /analytics/…`):
  - **Shared by every view:** the directory's search and filters plus `reporting_currency` (the same `DirectoryView` dependency as the directory and export). Active employees only — a `status` filter is ignored. Each employee's annual CTC comes from current compensation, converted through the latest stored USD rates. Every response carries `reporting_currency`, `rates_as_of` (the date of each rate used) and `excluded_no_rate` — active employees whose currency has no stored rate are counted, not silently dropped.
  - `/summary` — headcount, total cost, average and median, plus cost by country and by department with each one's share.
  - `/stats?group_by=department|country|title|level` — average, median (`percentile_cont`), minimum, maximum and headcount per group; levels by rank.
  - `/role-by-country` — title × level × country with headcount, average and median; narrow with `job_title_id` / `job_level_id`.
  - `/histogram?bins=` (1–100, default 20) — equal-width bins from lowest to highest, the maximum in the last bin.
  - `/outliers` — below 80% or above 120% of the peer median (same title, level, country; groups of at least 5), most extreme first by ratio. Peer groups are formed from the *filtered* population, so filtering to one department compares people only within it.
  - `/composition` — share of CTC per compensation-type category; types outside CTC are left out so the shares agree with the other views.
  - `/changes?date_from=&date_to=&limit=` — one event per employee per effective date, compared with the day before, in the employee's own currency (no rates involved). It counts toward the average increase unless it includes a correction, crosses a currency change, has no previous pay (a new hire), changes only types outside CTC, or is a relocation that leaves pay unchanged; each excluded event says why (`excluded_because`). The range stops at today. Counts and average cover every event; `items` is the newest `limit`.
  - Computed in SQL, not Python: window functions and `percentile_cont`; the change report reads the history once (each record gets the date it stopped applying via `lead()`), and returns the summary and the first page in one statement. Due future-dated records are promoted first, as on every read.
- **Done when (verified):** `tests/integration/test_analytics_api.py` (20 tests) builds a 12-person population with known pay in three currencies and compares every view with the pure definitions in `app.domain.analytics` worked out independently in Python — totals, averages, medians, grouped stats, histogram bins, outliers and their order, composition — plus the active-only rule, filters, an employee with no rate, a provider outage (previous rates keep working and their date is shown), and the change report's average increase with each exclusion reason. Removing the active-only rule fails 11 of them; removing the 5-person minimum fails the outlier test. On the 10k seed every view answers in 0.1–0.45 s (summary 163 ms, stats ~100 ms, role-by-country 199 ms, histogram 143 ms, outliers 232 ms, composition 127 ms, a full year's change report 440 ms). Past the deployed size these aggregate over every active employee (Scale Lab E3/E5): the next step there is `employee_totals` or materialized views.


## Phase 13 — Frontend (React/Vite) — Done
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
- **As built** (`frontend/`; see `frontend/README.md` for running it):
  - React 19 + TypeScript + Vite, `react-router-dom`, no UI library. In development Vite proxies `/api` to the backend, so no CORS is needed; a production build reads `VITE_API_URL` (hosting is Phase 15).
  - **Employees** (`/`): search (debounced), combinable department / country / title / level / status filters, sort by name, level, hire date or compensation, keyset paging with Previous / Next, local and reporting-currency totals, the rate date, and **Export CSV** — the view lives in the URL under the API's own parameter names, so the export link asks for exactly what is on screen.
  - **Profile** (`/employees/:id`): details, current breakdown (annualized, outside-CTC badge), total CTC in both currencies, full history with reason, note, who, previous amount and % change (a currency change shows "Currency changed", never a percent). Dialogs for **edit details**, **record a pay change** (any type and reason, future dates allowed and explained), **relocate** (optionally with a currency change), **change currency** (one amount per current component, all required; "Suggest from current rates" only pre-fills editable fields), and **terminate** (confirmation first; the record becomes read-only).
  - **Add employee**: required base pay, pay currency pre-filled from the country but independent of it.
  - **Import** wizard: template link → upload → report of every problem (row, column, reason) with no way to proceed while any exist → preview with names (not ids) → confirm with "imported by". If the data changed since the preview, the server's fresh report replaces it.
  - **Reference data**: tabs for compensation types (category suggestions from existing values, period picker with custom months, "counts toward total" toggle on by default, no base-pay option), change reasons (code filled in from the label), departments, job titles, levels. Duplicates and other server errors appear inline and keep what was typed. New entries appear in every dropdown immediately. There is no rename or delete.
  - **Pay insights** (`/analytics`): the seven §5 views, the directory's filters on top, the reporting currency selector in the header; each view shows the rate date and any employees left out for lack of a rate. Charts (cost bars, histogram, composition) are one-hue, label values at the tip, have tooltips, and each has a "View as table".
  - **Usability (§7):** one money/percent/date format everywhere (ISO currency code, not a symbol); inline validation before a request and the server's message after one; confirmation before termination; closing a dialog by clicking outside does nothing (a half-filled form isn't discarded by a stray click); focus is trapped in dialogs and Escape closes them; light and dark themes.
  - "Acting as" in the header is remembered in the browser and pre-fills every `changed_by` (there is no login).
- **Done when (verified):**
  - `npm test`: 59 tests (formatting, validation, URL state, API errors; components for the directory, import, reference forms and the pay-change, currency-change and terminate dialogs), with the guards removed on purpose to confirm the right tests fail.
  - A real Chromium drove the built app against the real API on the 10,000-employee seed — 19 flows in light and dark, all passing (search → filter → sort → page → export matches the on-screen rows; create, duplicate email, future-dated change, bonus, correction, relocation, a pending raise blocking a currency change with the server's message, a clean currency change, terminate; a bad import file reports and saves nothing, then a good one imports; reference forms; analytics filters and the reporting currency).
  - axe-core on all seven screens in both themes: no violations (it found and led to fixing: dark-mode button contrast of 4.41:1, content outside landmarks, and keyboard access to scrollable tables).
  - Every §1 success criterion that involves a screen is reachable without exporting to Excel: find a person and their history, answer each §5 question, no silent overwrite (history is read-only), import errors by row, and analytics that keep working on stored rates.
- **Found while testing in the browser:** `useAsync` flagged "loading" one render late, so stale rows briefly looked current — fixed at the source; filter checkboxes flickered because the router defers URL updates — they now update at once.
- **Not covered:** no automated end-to-end suite is checked in (the browser runs used a throwaway Playwright script against a live backend); adding one to CI belongs with Phase 14.


## Phase 14 — Non-functional hardening ✅ Done
- Re-run the 500 ms directory check (§1, §7) against the final schema/indexes with the full 10k seed and realistic filter combinations.
- Confirm append-only is structurally impossible to violate (no UPDATE/DELETE grants or code paths on `compensation_records`).
- Error handling and validation messages reviewed end-to-end (API error shapes → frontend display).
- Tests get their own database (the cross-cutting note below).
- **As built:**
  - **Append-only, four layers.** (1) Privileges: `app/db/grants.py` defines the least-privilege role (`python -m app.db.grants ROLE`, needs `APP_DB_PASSWORD`); `compensation_records` gets `SELECT, INSERT` only and no table anywhere gets `DELETE` or `TRUNCATE`. The app uses it by setting `RUNTIME_DATABASE_URL`; migrations keep using the owner `DATABASE_URL`. (2) The existing row trigger blocks `UPDATE`/`DELETE` even for the owner. (3) New migration `0012_no_truncate_records` adds a statement-level `BEFORE TRUNCATE` trigger — **this closed a real hole**: `TRUNCATE compensation_records CASCADE` (via another table) wiped history and row triggers don't fire on it. (4) Static tests scan `app/` (AST) for any `update`/`delete` on the model and check no route exposes PUT/PATCH/DELETE for records.
  - **Error contract.** Every handled error is `{"detail": str | [{loc, msg}]}`. Unexpected errors used to return plain text; now JSON handlers return 500 (`Something went wrong…`, never the exception text) and 503 for an unreachable database. The connection pool pings before use, so the API recovers after a database restart.
  - **Frontend.** `readable()` rewrites server sentences that name raw fields or ISO dates; a failed refresh keeps the old data on screen under a "may be out of date" banner instead of silently showing stale numbers; an unknown employee id shows a not-found page with a way back.
  - **Test database.** Tests run against `<db>_test` (auto-created, or `TEST_DATABASE_URL`), never the dev data. A guard fails any test that opens a non-loopback socket or makes an HTTP call.
  - **Index test.** `tests/integration/test_indexes.py` ties each directory filter and sort to an existing index, so dropping one fails a test.
- **Done when (verified):**
  - 500 ms re-check on the pristine 10,000 employees (`scale_lab/results/*phase14*`, details in `PERFORMANCE.md`): directory p95 80 ms; analytics p95 391 ms; the full unfiltered CSV export p95 639 ms (a file download, not the list/search/filter the target covers).
  - The whole API workflow was run as the restricted role, so the limited privileges are shown sufficient, not just restrictive. Each protection was mutation-checked (remove it, watch the test fail).
  - `test_error_contract.py` covers 18 failure classes; Playwright confirmed field errors, rule refusals, not-found, and database-down-then-recovered in the browser.
- **Found while testing:** analytics excluded everyone when no exchange rates were stored (same-currency amounts now need no rate); a contradictory "no rates were needed… left out" note; tests that silently depended on dev data; an earlier test of mine that proved nothing on an empty table.

## Phase 15 — Deployment ✅ Built, awaiting first deploy
- Hosted instance with managed Postgres, seeded on first run (per §9): `alembic upgrade head`, then the 1.2 seed commands (currencies/countries + first live rate fetch, companies, compensation catalog), then the Phase 3 seed. Both are idempotent.
- README with live URL and demo video at the top (per AI_USAGE.md's deployment decision).
- **As built:**
  - `app/bootstrap.py` (`python -m app.bootstrap`): migrations, reference data, catalog, change reasons, org structure and companies (each checked, all in one transaction), a first rate fetch whose failure only logs, then the 10,000 employees — skipped when employees exist, since history is append-only. Safe to run on every container start.
  - `app/web.py` (`uvicorn app.web:site`): one process serves the built frontend and mounts the API at `/api` (the prefix the dev proxy already uses, so the frontend needed no change and there is no CORS). Unknown non-API paths return `index.html` so deep links survive reloads; files outside the build directory are never served; `/health` is outside `/api` for the host's health check.
  - `Dockerfile` (Node build stage, then Python), `deploy/entrypoint.sh` (bootstrap, daily job in the background, server), `render.yaml` (Docker web service + managed Postgres), `.dockerignore`.
  - `DATABASE_URL` accepts the `postgres://` and `postgresql://` forms hosts hand out; `EXCHANGE_RATE_API_URL` defaults to the provider the app uses.
  - Root `README.md`: what is where, local run, deploy steps, optional least-privilege role, and the no-login caveat.
- **Done when (verified here):** a fresh empty database went from nothing to 10,000 employees in 9 s through `deploy/entrypoint.sh` exactly as the container runs it (rate provider unreachable, so the failure path was exercised), the daily job started, and a real Chromium loaded the list (50 rows), opened an employee, reloaded the deep link, and opened Pay insights with no console errors; running bootstrap again changed nothing. Tests: `test_bootstrap.py` (empty database → seeded instance, repeat start is a no-op), `test_deployment.py` (URL forms, SPA fallback, path traversal blocked — checked by removing the guard —, `/api` 404 stays JSON).
- **Not done (needs the user's accounts):** the Docker image was not built here (no Docker daemon in this environment) and nothing is deployed, so the live URL and demo video in the README are placeholders. Remaining steps: create the Render blueprint from the repo, wait for the first start, open the URL, then fill the README.

## Phase 16 — Scale Lab, Phase 2 experiments (post-submission, optional)
Per `docs/SCALE_LAB.md` E4–E8 (trigram search, materialized analytics, large-import `COPY` staging, index write cost, partitioning) — informs the §8 1M-path roadmap but isn't required for the 10k submission.

---

## Cross-cutting, continuous
- **Testing split (§7):** unit tests on Phase 2's pure functions (`tests/unit/`, fast, no infra); integration tests for API/DB behavior against the docker-compose Postgres (`tests/integration/`). The integration fixture runs `alembic upgrade head` once per session and wraps every test in a rolled-back transaction, so the dev DB is never polluted. Tests never call the live exchange-rate provider. Since Phase 14 they run against a dedicated `<db>_test` database.
- **AI_USAGE.md:** log Phase 3 entries as they happen (per the file's existing template) — what was generated, what was changed, what bugs were caught by tests.
- **Sequencing risk:** Phases 5 and 12 both depend on Phase 4's conclusions about the current-compensation strategy — don't hand-build either against an unvalidated assumption.
