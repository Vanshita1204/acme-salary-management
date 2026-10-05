# AI Usage — Key Prompts, Decisions, and Corrections

A curated record of how AI tools were used in this project: what they produced, what I accepted, changed, or rejected, and why. Raw session logs are not included (confirmed with Incubyte).

## Tools
| Tool | Used for |
|---|---|
| Claude (chat) | Requirements analysis, clarifying questions, scope decisions, design review |
| Claude Code (CLI agent) | Implementation, migrations, seed scripts, tests, refactoring, doc updates |

Instructions given to the coding agent are committed in _[e.g. `CLAUDE.md`]_.

## How I Worked with AI
- Requirements and scope were settled before any code was written; early scaffold code generated before scope was confirmed was discarded.
- AI suggestions were checked against the assessment brief and Incubyte's clarifications, not accepted on plausibility.
- Implementation went phase by phase against `docs/IMPLEMENTATION_PLAN.md`, and the plan, `DATABASE_DESIGN.md` and `REQUIREMENTS.md` were updated in the same step whenever a decision changed them.
- Each change was verified by running it, not by reading it: the test suite against a real Postgres (unit tests for pure domain logic, integration tests for constraints and the API), `alembic check` plus migration round-trips for schema changes, and live calls against the seeded database for endpoints.
- Where the right behavior was my call (reason model, CTC rules, migration strategy), the agent asked instead of guessing; where I disagreed with its default, I said so and it was redone (items 18, 20–22).

---

## Log

### Phase 1 — Requirements

**1. Clarifying questions overstated the brief**
- **Prompt:** Asked AI to draft clarifying questions about the assessment.
- **AI output:** Question framed the app as being for a "single HR Manager."
- **Correction:** The brief says "HR Manager," not "single." The question presumed an answer to the very thing I was asking. Rewritten neutrally.
- **Lesson:** AI paraphrase can quietly add assumptions; compare against the source wording.

**2. Question presumed a feature the brief never asked for**
- **AI output:** Asked what spreadsheet format the "import feature" should support.
- **Correction:** The brief only says data currently lives in Excel; import was the AI's inference. Reworded to ask whether migrating existing data is in scope. Incubyte confirmed it is optional.
- **Decision:** I included CSV import anyway, as the migration path off Excel, with all-or-nothing validation.

**3. AI recommended asking fewer questions**
- **AI output:** Suggested asking only the two questions that block design, defaulting the rest to save time.
- **Decision:** The brief explicitly says "do not make assumptions or proceed with doubts," so I sent the full set. Incubyte's answers changed scope: no authentication, structured analytics instead of AI Q&A, import optional.

**4. Comprehensive spec vs. one-page constraint**
- **Prompt:** Asked AI to make the requirements doc more comprehensive.
- **AI output:** Flagged that the brief asks for a one-page document.
- **Decision:** Split into a one-page summary (Part 1) and a detailed specification (Part 2) in the same file.

**5. Static vs. live exchange rates**
- **AI output:** Proposed a static exchange-rate table for deterministic analytics.
- **Decision:** I chose live rates, since HR needs current costs. AI identified the consequences: figures change without salary changes, dependency on an external API, and non-deterministic tests.
- **Resolution:** Rates fetched daily and stored; last stored rates used if a fetch fails; every analytics view shows "rates as of [date]"; tests use fixed rates and never call the API.

**6. Employees changing country**
- **AI output:** Assumed each employee has one country and is paid in its currency.
- **Correction:** Country can change. Country and currency moved onto each compensation record, so relocation adds a new record and history stays intact. Cross-currency increase reporting is planned separately.

**7. Deployment requirement**
- **My assumption:** Sharing the repo link meant deployment wasn't needed.
- **AI output:** Pointed to "Readiness: fully functional deployed software" in the brief, a separate requirement from the submission method.
- **Decision:** Deploy the 10,000-employee app on free tiers; live URL and demo video at the top of the README.

### Phase 2 — Design and Scale

**8. In-memory analytics would not scale**
- **AI output:** An early scaffold loaded all current salaries into Python to compute medians and group statistics.
- **Correction:** Acceptable at 10,000 rows, but it breaks at 10 lakh. Moved to SQL-side aggregation (`percentile_cont`), a denormalised current-compensation table, keyset pagination, and PostgreSQL instead of SQLite.
- **Follow-up:** Built a scale lab to test these decisions on 1M and 10M synthetic records (`docs/SCALE_LAB.md`).

**9. Fixed four-column compensation model didn't fit real pay structures**
- **Prompt:** Asked to add `company_name`/`termination_date` to employees, and to model compensation types (fixed, variable max, variable paid, equity, allowances with an allowance type, bonus with multiple types like quarterly/annual), each with its own change reasons.
- **AI output:** Replaced the four fixed columns (`base_salary`/`annual_bonus`/`allowances`/`annual_equity_value`) and the single global `change_reason` enum with a `compensation_types` catalog + a `change_reasons` table scoped per type, and restructured `compensation_records`/`current_compensation` to one row per (employee, type).
- **Decision:** Kept this shape — it generalizes to an arbitrary number of pay components instead of hardcoding four, which the brief's original "base + bonus + allowances + equity" phrasing didn't anticipate.

**10. AI over-constrained a catalog meant to be user-extensible**
- **Prompt:** Asked for `category`/`subtype` on compensation types to be "choice fields."
- **AI output:** Added `CHECK (category IN (...))` and a compound `CHECK` restricting `subtype` to a fixed list per category.
- **Correction:** Reversed — compensation types are added manually, so a hardcoded value list was wrong. Replaced the subtype-choice idea with `period_months` (an integer, not a time-period string) so any type can be annualized the same way (`amount * 12 / period_months`), and added an `is_base_pay` boolean so the "base salary > 0" rule doesn't depend on `category` staying a specific string.
- **Lesson:** When a rule needs a stable signal (which type is "base pay"), use a dedicated column, not a convention on a free-text field that's explicitly meant to be edited.

**11. Company-scoped catalog, then reversed to universal**
- **Prompt:** Earlier instruction read as "the compensation type can be unique for a company, not employee," which AI interpreted as per-company scoping (`compensation_types.company_id`).
- **Correction:** Later instructed to "make it universal" — `company_id` and its FK removed from `compensation_types` entirely; one shared catalog across all companies.
- **Lesson:** An ambiguous scoping instruction ("unique for a company") should have been confirmed with a direct question before building a company-scoped schema, rather than guessing and redoing it once the guess proved wrong.

**12. Country and currency are not the same thing**
- **Prompt:** "currency should be on employee table... I can be in Dubai and still want to be paid in USD" — pushing back on an implicit assumption that pay currency derives from country.
- **Correction:** Added `employees.currency` as its own field, independent of `employees.current_country` — no FK/CHECK ties them. `countries.default_currency` demoted to a seed-script convenience only, never a constraint.
- **Lesson:** Two real-world-correlated fields (where someone is based, what currency they're paid in) aren't the same field; don't collapse them just because they usually agree.

**13. Rejected auto-converting compensation amounts on a currency change**
- **Prompt:** "current compensation doesn't need to have country and currency, employee table already has it" — then, when AI proposed auto-converting old amounts via the latest exchange rate on a currency change: "we don't know which conversion rate [to use]."
- **AI output (rejected):** A trigger/service function that converted every current compensation amount into the new currency automatically using the latest stored exchange rate.
- **Correction:** There's no principled choice of *which* rate (today's vs. each record's original date), and more importantly, auto-converting would make a permanently-recorded compensation figure depend on a live external rate at the moment of the edit — contradicting the existing "never depend on the exchange-rate provider" resilience principle, which until then had only been applied to reads. Replaced with a required HR-entry screen: one editable amount per current compensation type, submitted together as one transaction, no system-computed conversion.
- **Lesson:** A stated resilience principle ("don't depend on the rate provider") has to be checked against *every* new feature that touches rates, not just the ones that look like reads.

**14. Confirmed, not changed: cross-currency comparisons use one shared rate snapshot**
- **Prompt:** Asked whether comparing two employees' pay in different currencies (e.g. $20k vs. ₹25L) requires knowing the exact date each salary was credited.
- **AI output:** Explained that using each employee's own `effective_date` rate would compare two different points in currency history (apples to oranges); the correct approach — already specified in §5/FR-8 — is one current rate snapshot applied uniformly to every employee being compared, with the rate date displayed.
- **Decision:** No schema or spec change; confirmed the existing design already prevents this bug, rather than needing a fix.

### Phase 3 — Implementation

**15. Hardcoded connection strings moved to `.env`, and a Postgres gotcha it exposed**
- **Prompt:** "extract all urls from .env. remove hardcoding"
- **AI output:** Made `DATABASE_URL` a required setting (no in-code default), removed the URL from `alembic.ini`, and had docker-compose read the Postgres credentials from `backend/.env`.
- **Follow-up bug:** After the password in `.env` was changed, every connection failed with "password authentication failed." Postgres only applies `POSTGRES_PASSWORD` when the data volume is first created, so the running database still had the old password. Fixed with `ALTER ROLE`, and documented in the plan.
- **Lesson:** Config that's read once at initialization (a database volume) doesn't follow later `.env` edits.

**16. Migrations: AI edited a pushed migration, then the project chose to fold fixes back in**
- **AI output:** To widen `exchange_rates.rate_to_usd`, the AI first edited migration `0009` in place, assuming it wasn't committed. It then saw `0009` had already been pushed, reverted the edit, and added a follow-up migration (`0010`), later a second one (`0011`) for change reasons.
- **Decision:** No database outside development had been migrated yet, so I asked for the follow-up migrations to be removed and the fixes folded into the create migrations (`0005`, `0007`, `0009`). The dev database was rebuilt from scratch.
- **Lesson:** Editing applied migrations is normally wrong, but it's the cleaner choice before any shared database exists. Decide it explicitly; don't let it happen by accident.

**17. Exchange-rate precision bug caught from real data**
- **AI output:** The schema stored `rate_to_usd` as `NUMERIC(18,8)`. Before wiring up storage, the AI checked the live provider's quotes and found the Iranian rial (~1.48M per USD) would be stored as 0.00000068 instead of 0.000000676 — a 0.7% error on every conversion.
- **Correction:** Widened to `NUMERIC(24,16)` (~9 significant digits for every currency), with a unit test on round-trip precision.
- **Lesson:** Check numeric precision against the extreme values in real data, not typical ones.

**18. Country and currency lists: hand-picked → generated file → third-party packages at runtime**
- **AI output:** First seeded 18 hand-picked countries and 14 currencies, then generated the full ISO list from pycountry + babel once and committed it as a static file.
- **Correction:** I asked for the lists to come from the packages directly, so updating means upgrading a package rather than editing data. `app/seed/iso_data.py` now reads pycountry (codes, currency names) and babel/CLDR (country names, each country's legal tender) when seeding.
- **AI caught:** babel's CLDR data still listed Bulgaria as BGN after it adopted the euro in January 2026 (pycountry had already dropped BGN). Handled with a small, documented `CURRENCY_OVERRIDES` entry.

**19. Live exchange rates without an API call per dashboard refresh**
- **Prompt:** "implement a live currency converter", then "i dont want api call each time dashboard is refreshed"
- **AI output:** Conversions already read only stored rates (per the resilience requirement). The remaining gap was a dashboard calling the refresh endpoint on every load.
- **Decision:** Refresh is throttled to once per 24 hours (the provider publishes daily) and returns `skipped: true` otherwise; `force` overrides. Tests fake the provider with `httpx.MockTransport` and use far-future rate dates, so they never depend on the live API or collide with real stored rates.

**20. Change reasons belong to the record, not the compensation type**
- **AI output (earlier design, item 9):** Change reasons were scoped per compensation type and enforced with a composite foreign key.
- **Correction:** "change reason should not be compensation type dependent. it should be record dependent." Replaced with one shared `change_reasons` list any record can use. I chose a lookup list over free text because rules depend on specific codes (`new_hire`, `relocation`, `correction`); a seed smoke check guarantees those exist.
- **Lesson:** Item 9 had read "each type has its own change reasons" too literally; the reason describes why a record changed, which is independent of what kind of pay it is.

**21. Reimbursements and total compensation (CTC)**
- **AI output:** Raised an open question: should reimbursement types count toward total compensation? It recommended excluding the whole `reimbursement` category.
- **Correction:** "some companies involve reimbursement for wifi for example in the ctc." A category-wide rule can't fit both kinds of employer. Added a per-type `counts_toward_total` flag (base pay must count, enforced by a check constraint), with a toggle in the planned "Add compensation type" form, and seeded both an in-CTC and an outside-CTC internet reimbursement.

**22. Per-constraint error messages → generic templates**
- **AI output:** The API translated database constraint violations into 409/422 responses using a hand-written message for each constraint name.
- **Correction:** "use constants etc. dont hardcode error message for each model." Messages are now a few templates (`UNIQUE_MESSAGE`, `FOREIGN_KEY_MESSAGE`, `CHECK_MESSAGE`, …), filled in from the SQLAlchemy metadata of whichever constraint failed. `Base` uses Postgres's default constraint naming so model and database names match.
- **Bug caught by tests:** The first version checked `psycopg.errors.IntegrityConstraintViolation`, but psycopg's specific violations inherit from `IntegrityError` directly, so every violation escaped as a 500. The API tests failed immediately; fixed by checking `psycopg.IntegrityError`.
- **Trade-off accepted:** check-constraint messages are less specific ("employee rule violated: termination after hire").

**23. Smaller bugs the agent introduced or hit, and how they were caught**
- **Alembic revision ID too long:** `0010_widen_exchange_rate_precision` exceeded `alembic_version.version_num`'s 32 characters; the upgrade failed and rolled back cleanly. Renamed.
- **Migrations silenced app logging:** `alembic`'s `fileConfig()` disabled existing loggers, so a test asserting a "keeping latest stored rates" warning saw nothing. Fixed with `disable_existing_loggers=False`.
- **Validation order:** a change-reason code with capitals was rejected instead of lowercased, because pydantic checked the pattern before normalizing. Caught by an API test.
- **Formatter noise:** running `ruff format` reflowed committed model files that had been written at a wider line length. The AI reverted the formatting and kept only real changes.
- **Stray folder:** a nested, empty `acme-salary-management/` with two empty git repos, left by a setup command run from inside the project.

**24. Verification habits that paid off**
- Every migration change was checked with `alembic check` (no drift between models and schema) and a full `downgrade base` → `upgrade head` round-trip.
- The change-reason merge migration (later folded away) was proven on a throwaway database with a real record before relying on it, because it temporarily bypassed the append-only trigger.
- Integration tests run inside a rolled-back transaction and use ISO's reserved test codes (`XTS`, `XA`), so they never pollute or depend on the dev database.
- A unit test parses every `app/domain/` module and fails on any database, HTTP or API import, enforcing the "pure functions" rule mechanically.

---

## Entry Template
**N. Short title**
- **Prompt:** what I asked
- **AI output:** what it produced
- **Correction / Decision:** what I changed or chose, and why
- **Lesson:** (optional) what I'd do differently
