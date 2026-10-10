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
- Implementation went phase by phase against `docs/IMPLEMENTATION_PLAN.md`, and the plan, `DATABASE_DESIGN.md`, `REQUIREMENTS.md` and `SPECIFICATION.md` were updated in the same step whenever a decision changed them.
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
- **Decision:** Split into a one-page summary (Part 1) and a detailed specification (Part 2) in the same file. Later moved into two files: `REQUIREMENTS.md` (one page) and `SPECIFICATION.md`.

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

**24. Verification habits that paid off (Phases 1–1.3)**
- Every migration change was checked with `alembic check` (no drift between models and schema) and a full `downgrade base` → `upgrade head` round-trip.
- The change-reason merge migration (later folded away) was proven on a throwaway database with a real record before relying on it, because it temporarily bypassed the append-only trigger.
- Integration tests run inside a rolled-back transaction and use ISO's reserved test codes (`XTS`, `XA`), so they never pollute or depend on the dev database.
- A unit test parses every `app/domain/` module and fails on any database, HTTP or API import, enforcing the "pure functions" rule mechanically.

**25. Domain layer: two spec gaps resolved explicitly**
- **AI output:** While writing the Phase 2 pure functions, the agent hit two places the requirements didn't settle and flagged both instead of guessing silently.
- **CSV template:** FR-5 listed a single "name" column, but the employee model stores first and last name separately. The template uses `first_name` / `last_name`, and `SPECIFICATION.md` was updated to match.
- **Average increase across a currency change:** excluded, alongside corrections. Comparing totals in two currencies isn't meaningful, and FR-7 already defers cross-currency increases.
- **Enforced mechanically:** a unit test parses every `app/domain/` module and fails on any database, HTTP or API import.

**26. Seed realism checked against the data, including a false alarm**
- **AI output:** After generating 10,000 employees, the agent queried medians per country and currency instead of trusting the generator. Singapore employees paid in USD showed the same median as those paid in SGD, which looked like a missing conversion.
- **Verification:** breaking it down by level and department showed USD pay consistently ~0.74× SGD, as intended. The medians matched only because those 21 people had a different level mix. No change made.
- **Lesson:** an aggregate that looks wrong needs a finer cut before it's called a bug — and before it's called fine.

**27. Scale Lab: the seed had to change before it could be measured**
- **AI output:** The seed built every employee and record in memory before inserting. At 1M employees that would be ~18M record dicts, many gigabytes. The agent rewrote it to stream in 5,000-employee chunks, committing each, and made the insert method selectable so E1 could compare row-by-row, ORM, batched `INSERT` and `COPY` on identical data. A checksum confirmed the methods wrote the same rows.
- **Decision:** `COPY` became the seed default (1.7× batched `INSERT`, 7.9× row-by-row).

**28. A real bug only volume could find: employee codes collided at hire #1,000,000**
- **What happened:** The first 1M load crashed: `duplicate key value violates unique constraint "employees_code_key" — Key (code)=(EMP-100000)`.
- **Cause:** codes were generated with `lpad(n, 6, '0')`, and Postgres's `lpad` *truncates* longer strings — `lpad('1000000', 6, '0')` is `100000`. The production app would have failed on its millionth hire. No test at 10k could have shown it.
- **Fix:** an `employee_code()` function that pads to at least 6 digits and never truncates, folded into migration `0006` (fresh database), plus a boundary test (42, 999,999, 1,000,000, 12,345,678).
- **Lesson:** this is the case for running the lab at all — the most valuable result wasn't a timing.

**29. Recording a limit, and softening an unmeasured claim**
- **AI output:** E3 confirmed the `current_compensation` table (3–4× faster for whole-population queries) and that the composite history index is essential (a page goes from 1.8 ms to 2.9 s without it at 1M). It also found that, even with the table, sorting by compensation and country aggregates take 2.9–4.2 s at 1M.
- **Decision:** recorded as a limit, not hidden. It's fine at the deployed 10k (22–61 ms); scaling past that needs an indexed per-employee totals table or materialized analytics (E5).
- **Correction:** the first draft of `PERFORMANCE.md` explained `COPY`'s smaller-than-expected lead as the per-row validation trigger's cost. That wasn't measured, so it was reworded as the likely reason, with the actual test left to E7.

**30. Starting Phase 5 before Phase 4 finished**
- **Prompt:** "next phase" while the 1M experiments were still running.
- **Decision:** Phase 5 (directory API) depends on Phase 4's pagination and current-compensation conclusions. The 10k results already pointed the same way as the original design, so the agent built Phase 5 on that and said it would revisit if 1M disagreed. 1M confirmed both (keyset < 1 ms vs `OFFSET` 2.6 s).
- **Design call surfaced:** "sort by compensation" across employees paid in different currencies can't compare raw totals. The sort compares USD-equivalent totals at the latest stored rates, in SQL, which gives the same order for every reporting currency.
- **Bug caught in review:** a cursor that decoded but held an unparseable value would have returned a 500. Fixed to a 400, with tests for tampered and mismatched cursors. The directory met its acceptance target: p95 42 ms at 10k (target < 500 ms).

**31. Future-dated raises: "check on every read" questioned, then a daily worker**
- **AI output:** For FR-4, the plan's "recompute-on-read" was built literally. Every read of current pay (directory, profile) first switches future-dated records whose date has arrived. A partial index over records saved with a future date keeps the check small.
- **Prompt:** "if i run an aggregation, do i check every time" — e.g. top-paid in one role across 10,000 employees.
- **AI explanation:** The check isn't per employee. It's one query over the small index of pending future-dated records, ~2 ms on the 10k seed whether the aggregation touches 10 employees or 10,000. Effective dates are whole days, so once a day is enough.
- **Decision:** I was fine adding a worker. Phase 8's daily job now refreshes exchange rates and switches due records. The read-time check stays as a safety net for a late or missed run; with the job running it finds nothing.
- **Lesson:** "Recompute on read" in a plan should state its cost and frequency, or it reads as "recompute the world on every request."

**32. Bugs the tests caught in Phase 7**
- **Row count:** the promotion upsert's `rowcount` came back as -1 through SQLAlchemy/psycopg. Switched to counting `RETURNING` rows.
- **Global effects in tests:** promotion is company-wide, so an assertion like "exactly one record promoted" failed once the dev database held another pending raise. Tests now assert on their own employee's pay.
- **Commit placement:** saving promotions with `commit()` before building the profile response would expire every loaded ORM object and reload each history row with its own query. The commit moved after the response is built.

**33. No table for department, title or level**
- **Prompt:** "why is there no table for role"
- **AI output:** Admitted there was no recorded reason. These were free text since Phase 1, which contradicts SPECIFICATION §1 ("inconsistent department or title names go uncaught"). "Software Engineer" and "software engineer" would split grouped statistics and outlier peer groups, and levels sorted as text (`L10` before `L2`).
- **Decision:** Inserted as Phase 7A (not renumbered, since later phase numbers are referenced throughout code and docs), before CSV import and analytics, which both depend on it:
  - case-insensitive unique lookup tables;
  - titles shared across departments, like the compensation-type catalog;
  - levels with a `rank` so they sort correctly.
- **Verification:**
  - The migration ran in place on the 10k dev database; every employee's values matched a pre-migration export row by row, and downgrading restored identical text.
  - A throwaway database proved case variants (`" engineering "`, `l2`) merge and `L10` ranks after `L2`.
  - The regenerated seed was identical to the pre-7A one.
- **Lesson:** "Why isn't X modelled?" deserves a straight "it should be" when the requirements already say so, not a rationalisation.

**34. A latency number that looked like a regression**
- **AI output:** After 7A, the directory benchmark showed p95 73.5 ms against the 42 ms recorded in Phase 5.
- **Verification:** Instead of attributing the gap to 7A, the agent ran the pre-7A code on the same machine and data: 65 ms before, 70 ms after. Most of the gap was the machine; 7A costs ~5–10%, still far under the 500 ms target.
- **Decision:** Kept the original Phase 5 result file and recorded the new before/after pair separately, so measurements from different machines aren't mixed.

**35. Phase 8: a throttle that would have skipped days**
- **AI caught:** The refresh endpoint skips if rates were fetched within 24 hours. A job scheduled for 00:10 daily would skip whenever yesterday's run finished a few seconds later in the day, leaving rates a day stale. The job now refetches after 1 hour (only to stop a crash-looping worker hammering the provider); the API keeps 24 hours.
- **Gap closed:** an unknown reporting currency used to return empty conversions silently; it's now a 422. A supported currency with no rate still returns `null` rather than failing the page.
- **Verification limit:** the live provider was blocked from the build environment (403). That exercised the failure path for real (rates kept, records still promoted, non-zero exit), but a successful live fetch is covered only by tests with a faked provider.

**36. Phase 9: CSV import**
- **Decision:** confirm validates the file again against the current database instead of trusting the earlier preview. A test adds one of the file's employees by hand between preview and confirm, and the confirm reports that row.
- **AI output:**
  - Inserts are set-based (employees, records, current pay in three statements): a 10,000-row file validates in 1.1 s and confirms in 4.2 s.
  - Existing-email checks look up only the file's own emails, so they don't grow with the employee table.
- **AI caught:** an early version stopped reading after 10,001 rows to save work, which would have made the "file has N rows" error report the wrong N. Reading is bounded by the 10 MB upload limit instead.
- **Verification:** the plan's acceptance case (a file with one bad row saves nothing and reports the row, column and reason) is a test, along with a rollback test that fails after employee rows were already inserted.

**37. Phase 10: the export has to be the same view, not a lookalike**
- **AI output:** The export takes exactly the directory's parameters through one shared dependency and runs the directory's own filter and sort statement without the page limit, instead of a second query that happens to resemble it.
- **Verification:** 13 tests page through the directory two rows at a time and compare the result with the export for six views (every sort in both directions, filters, search, three reporting currencies): same people, same order, same totals. Reversing the export's sort order fails all six.
- **AI added unasked:** text cells starting with `=`, `+`, `-`, `@` are prefixed so a spreadsheet doesn't run them as formulas, and the file carries a byte-order mark so accented names display correctly in Excel.
- **Limit recorded:** the file is built in memory (1.35 MB at 10,000 employees). Past the deployed size it should stream.

**38. Phase 11: "no type stranded in the old currency" needed more rules than the plan listed**
- **Prompt:** the plan's done-when: a currency change leaves no compensation type in the old currency.
- **AI output:** Meeting that required rules the plan didn't state. The amounts must name exactly the employee's current types. The effective date can't be in the future or before a current record, because the new record would otherwise never become current while the employee row had already moved. A currency change is refused while future-dated records exist. A last-line check rolls the whole transaction back if any rewritten type didn't become current.
- **Verification:** a SQL check that no current row is in a currency other than the employee's runs after every successful and every rejected attempt, and a failure on the second of three records rolls back the currency change.
- **Decision:** no automatic conversion. The live rate is only a suggestion that pre-fills editable fields.

**39. Phase 12: checking the analytics against a second definition**
- **AI output:** Every view is computed in SQL (`percentile_cont`, window functions). The tests build a 12-person population with known pay in three currencies and compare each view with pure Python definitions worked out separately.
- **Verification:** removing the active-only rule fails 11 tests and removing the 5-person minimum fails the outlier test, so the tests do check those rules.
- **Design call:** employees whose currency has no stored rate are counted in `excluded_no_rate` instead of silently dropped. Phase 14 later found a case this missed (item 41).

**40. Phase 13: what the browser found that the tests didn't**
- **Verification:** besides 59 component tests, a real Chromium drove the built app against the real API on the 10,000-employee seed (19 flows, light and dark), and axe-core audited all seven screens.
- **Bugs found:** the loading flag flipped one render late, so stale rows briefly looked current; filter checkboxes flickered because the router defers URL updates; the dark-mode primary button was 4.41:1 contrast; content sat outside landmarks; scrollable tables weren't keyboard-reachable. All fixed.
- **Limit recorded:** the browser runs were a throwaway script, not a checked-in end-to-end suite.

**41. Phase 14: the hardening pass found a real hole and weak tests of my own**
- **AI caught:** a statement-level `TRUNCATE compensation_records CASCADE` (via another table) wiped history, because row triggers don't fire on `TRUNCATE`. Migration `0012` adds a trigger for it. A least-privilege database role and static code checks cover the other paths.
- **Tests that proved nothing:** my owner `UPDATE`/`DELETE` tests passed on an empty table because there was nothing to modify ("DID NOT RAISE" once rows existed), and my first mutation check was meaningless because the test session re-ran the migration that I was removing. Both were redone so the mutation actually fails the test.
- **Test isolation:** tests moved to their own database with a network guard. The guard first missed outbound HTTP through a local proxy; an HTTP transport patch closed it.
- **Real product bugs found:** a failed refresh left stale numbers on screen with no warning; analytics excluded everyone when no exchange rates were stored (same-currency pay needs no rate); dead pooled connections after a database restart; raw field names in server messages.
- **Honest result:** the full unfiltered CSV export took 639 ms (p95), over 500 ms. The requirement covers list, search and filter, not file download, so I recorded it with its profile instead of quietly changing the target. Directory p95 was 80 ms and analytics 391 ms.

**42. Phase 15: deployment I could not do, and did not pretend to**
- **Limit:** the agent environment has no Docker daemon and no hosting account, so the image was not built and nothing is deployed. The README's live URL and demo video are marked as placeholders to fill in after the first deploy.
- **What was verified:** the container's own entrypoint script took an empty database to 10,000 employees in 9 s with the rate provider unreachable, served the app, and a real browser loaded the list, a deep link after reload, and Pay insights. A second start changed nothing.
- **AI caught:** hosts hand out `postgres://` URLs, which the psycopg 3 driver doesn't accept without the driver name; the setting now normalises them.
- **Design call:** the API is mounted under `/api` (the prefix the dev proxy already used) so the frontend needed no change and no CORS.

**43. Phase 16: three results that went against my hypotheses, and one that looked like a failure**
- **Prompt:** run the plan's remaining Scale Lab experiments (search, analytics, import, index cost, partitioning).
- **Hypotheses disproven:** zero indexes was not faster to load, it was 4.4× slower, because the seed reads back what it wrote; the staging import was 1.8–2.5× faster, not "several times"; extra indexes cost 3.7% load time.
- **Looked like a failure:** the trigram index showed no speedup at 1M. The AI didn't accept that. The plan captured by `EXPLAIN` used the index; the timed runs didn't. Cause: the driver prepares a statement after five runs and Postgres then uses one generic plan that can't know a term is rare. The same search went from 2 ms to 4 s. That is a real hazard for the app, recorded with its fix instead of the index being declared useless.
- **Also found:** an unanalysed staging table made validation 5–8× slower (fixed); `work_mem = 256MB` made 1M-row validation 8× slower and the cause is unexplained, so it is recorded as such.
- **Deviation recorded:** the plan's 10-million-employee dataset was not run (disk and hours); partitioning was measured at 1 million instead.
- **Caught before it landed:** rebuilding the lab databases overwrote the earlier Apple-Silicon build results that the docs cite. They were restored and the new ones saved under a different name.

---

## Entry Template
**N. Short title**
- **Prompt:** what I asked
- **AI output:** what it produced
- **Correction / Decision:** what I changed or chose, and why
- **Lesson:** (optional) what I'd do differently
