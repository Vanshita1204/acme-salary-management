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

## Phase 1 — Foundation: models, reference data, basic CRUD
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
- `compensation_types`: the starting universal catalog in `app/seed/compensation_types.py` (`python -m app.seed.compensation_types`; edit `CATALOG` and re-run to extend) — 12 types:
  - fixed: Base Pay (`is_base_pay = true`, monthly)
  - variable: Annual Variable Pay Target (12)
  - bonus: Quarterly Bonus (3), Annual Bonus (12), Retention Bonus (12)
  - equity: Annual Equity Grant RSU (12)
  - allowance: Housing, Transport, Meal (all monthly)
  - reimbursement: Phone & Internet (1), Learning & Development (12), Wellness (12). The recorded amount is the employee's entitlement/cap for the period, not individual expense claims (those belong to an expenses system, out of scope).
  - Seeded types are only a starting point — HR adds more from the UI (1.3 + Phase 13). Covered by `tests/integration/test_compensation_types_seed.py`.
- `change_reasons`: one shared list, **not tied to compensation types** — any record of any type can use any reason (`app/seed/change_reasons.py`, `python -m app.seed.change_reasons`). 11 reasons: new hire, annual revision, promotion, market adjustment, role change, relocation, bonus payout, equity grant, retention award, policy change, correction. The smoke check requires `new_hire`, `relocation` and `correction`, which business rules look up by code. Covered by `tests/integration/test_change_reasons_seed.py`.
- `exchange_rates`: **live rates, not fixtures** — `app.seed.reference` finishes by calling Phase 8's refresh (below) against the real provider. A provider outage doesn't fail the reference load; it just warns.
- **Seeding order:** `alembic upgrade head`, then `python -m app.seed.reference` (currencies, countries, live rates), `python -m app.seed.companies`, `python -m app.seed.compensation_types`, `python -m app.seed.change_reasons`. Each is idempotent.
- **Done when (verified):** after 1.1's migrations and the loads above, every lookup table is populated and the smoke checks pass — every country's `default_currency` resolves (`check_reference_data`), exactly one base-pay type exists (`check_compensation_types`), and the reasons the rules rely on exist (`check_change_reasons`). Each loader is also tested to insert nothing on a second run.

### 1.3 Basic CRUD
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
- **Done when:** every model can be created and read back through the API against the pre-populated reference data from 1.2, confirming the schema works end-to-end before any domain logic or business workflow is layered on.

## Phase 2 — Domain layer (pure functions, no DB/network)
Lives under `backend/app/domain/`, unit-tested under `backend/tests/unit/`.
- Compensation: `annualize(amount, period_months)` (= `amount * 12 / period_months`), total-compensation calc (sum of annualized current types — **open decision:** whether `reimbursement`-category types count toward total compensation, or are reported separately since they're expense entitlements rather than pay; decide before Phase 5's directory totals), percentage change between two records of the same type (correction-aware per §5 definitions).
- Currency — **done early, with Phase 8's rate work** (`app/domain/currency.py`): `convert` / `cross_rate` through USD rates (non-USD → non-USD goes via USD, per FR-8), rounded half-even to cents; `rates_to_usd_from_usd_base` inverts the provider's units-per-USD quotes via `str()` so float noise doesn't leak into `Decimal`. Unit-tested in `tests/unit/test_currency.py`. Open: zero-decimal currencies (JPY, KRW, …) still round to cents — use ISO 4217 minor units when the UI formats amounts.
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
