# AI Usage

How I used AI on this project, what I steered, and where I overruled it. I used Claude (chat) for the requirements questions and Claude Code for the implementation. Raw session logs are not included (confirmed with Incubyte).

## How I worked

- **Plan first.** I settled scope and the data model before building. Scaffold code the AI produced before that was thrown away.
- **I designed the data model myself, one prompt at a time.** The AI drafted; I pushed back until the model matched how pay actually works (items 4–12 below).
- **From the schema on, I ran the build phase by phase** against `docs/IMPLEMENTATION_PLAN.md`. I told the agent which phase to do, asked it to explain what it had done, checked on long-running jobs, and kept the plan and spec in sync when a decision changed.
- **I checked claims against the brief and the code.** The brief, not the AI's paraphrase of it, was the reference.
- **Verification was by running things:** the test suite against a real Postgres, `alembic check` and migration round-trips for schema changes, and live calls against the seeded database.

## What I decided and what the AI did

| I decided | The AI did |
| --- | --- |
| Live exchange rates, never auto-converting stored pay, deploying the app | Laid out the consequences of each option |
| The compensation model: types as a catalog, `period_months`, counts-toward-CTC flag, currency independent of country, change reasons on the record | Wrote schema and migrations, redone after each of my corrections |
| Migration policy: fold fixes into the create migrations while the DB is new | Rewrote the migrations |
| Moving on phase by phase, and what a phase must do | Wrote the code and tests for each phase |

---

## Planning (my prompts and corrections)

**1. Live vs static exchange rates.** The AI proposed a static table. I chose live rates because HR needs current costs. Consequences, settled together: rates fetched daily and stored; the last stored rates used if a fetch fails; every analytics view shows "rates as of"; tests use fixed rates and never call the API.

**2. Employees can change country.** The AI assumed one country per employee. I corrected that, so relocation is a new dated record and history stays intact.

**3. Scale.** An early scaffold loaded every salary into Python to compute medians. That works at 10,000 rows and breaks at a million. I had it moved to SQL aggregation, a current-compensation table, keyset pagination and PostgreSQL instead of SQLite, and I had a scale lab built to test those choices (`docs/SCALE_LAB.md`).

**4. Compensation types.** My prompt: add company name and termination date to employees, and make compensation types a table: fixed, variable (max and paid), equity, allowances, bonus of several kinds, each with its own change reasons. The AI replaced the four fixed pay columns with a type catalog and one record per employee per type.

**5. Types are not a fixed list.** The AI added `CHECK` constraints limiting category and subtype to fixed values. I said types are added manually and the point is just to make calculation easy. I wanted category kept, subtype optional, and the period expressed as a number of months. That became `period_months` (annual amount = `amount * 12 / period_months`) plus an `is_base_pay` flag, so the base-pay rule doesn't depend on a text value.

**6. Company-scoped, then universal.** My wording ("unique for a company, not employee") was read as a per-company catalog. I then said "make it universal" and the company link was removed. I should have clarified before the first version.

**7. Country is not currency.** I said currency belongs on the employee, and that I can live in Dubai and be paid in USD. `employees.currency` is independent of country; a country's default currency is only a seed convenience.

**8. No auto-conversion on a currency change.** The AI proposed converting existing amounts with the latest rate. I rejected it: there's no right rate to pick, and a stored salary shouldn't change because of a live rate at edit time. Instead the currency-change screen shows an amount field per current compensation type, saved in one transaction.

**9. Comparing pay across currencies.** I asked whether comparing $20k and ₹25L needs the date each salary was paid. The answer: use one current rate snapshot for everyone, and show its date. No design change.

**10. Two questions I asked before accepting the design.** Why two tables (`compensation_records` and `current_compensation`)? The history table is append-only and the current table is a fast lookup (E3 later measured the gain). And I asked to remove `country` and `currency` from the current table, since the employee row already has them.

**11. Change reasons belong to the record.** Reasons were tied to compensation types. I told it they should depend on the record, so there is one shared list. I chose a lookup list rather than free text because rules depend on specific codes (`new_hire`, `relocation`, `correction`).

**12. Reimbursements and CTC.** The AI recommended excluding all reimbursements from total compensation. I pointed out that some companies include a wifi reimbursement in CTC, so it became a per-type `counts_toward_total` flag (base pay must count), with a toggle in the planned "Add compensation type" form.

---

## Building

**13. Config out of the code.** I asked for all URLs and credentials to come from `.env`. A side effect: changing the Postgres password in `.env` didn't change the running database, because Postgres reads it only when the volume is first created. Fixed with `ALTER ROLE` and noted in the plan.

**14. Migrations.** The AI first edited an already-pushed migration, then added follow-up migrations. Since no shared database existed, I told it to remove the follow-ups and fold the fixes into the create migrations, then re-run from scratch.

**15. Country and currency data from packages.** I asked for the lists to come from pycountry and babel at seed time, so updates mean upgrading a package. The AI noticed babel still listed Bulgaria as BGN after the euro switch in January 2026, and added a small documented override.

**16. No API call per dashboard refresh.** I asked for a live converter, then said I didn't want an API call on every dashboard refresh. Conversions read stored rates; the refresh is throttled to once a day.

**17. Reviewing the agent's work.** I checked the file layout and asked why there was a nested folder (the agent had left a stray empty repo, which was removed). I asked "frontend not required?" to confirm it was a later phase, and "explain what you did in phase 4 and 5 properly" before moving on. I also asked how many rows the 1M load had left, which is how I saw its `PERFORMANCE.md` row was still "pending".

**18. Housekeeping I did myself.** I added the evaluation-only `LICENSE` and resolved the merge conflicts. I pointed my editor at `backend/.venv` to clear the import warning.

**19. Error messages.** The API had a hand-written message per database constraint. I told it not to hardcode messages per model; it now uses a few templates filled from the constraint's metadata. The first version had a bug (psycopg's integrity errors weren't caught, so every violation returned 500) that the API tests caught at once.

---

## Phases 2–16 (agent-built against the plan)

I ran these phases with the agent, one at a time. The findings below were recorded in the repo as the work went, and each is covered by tests or by a note in `docs/PERFORMANCE.md`.

- **Domain layer (Phase 2).** Pure functions with a test that fails if any domain module imports the database or HTTP code. Two spec gaps were flagged and resolved instead of guessed (the CSV `first_name`/`last_name` columns; excluding currency changes from average-increase figures).
- **Seed and Scale Lab (Phases 3–4).** The seed was rewritten to stream in chunks so it could reach 1M employees. The 1M run found a real bug: employee codes collided at hire #1,000,000, because Postgres's `lpad` truncates. Fixed with an `employee_code()` function and a boundary test.
- **Limits recorded, not hidden.** At 1M, sorting by compensation and country aggregates take 2.9–4.2 s even with the current-compensation table. That's fine at the deployed 10k (22–61 ms), and the fix is written down.
- **Directory, profile, compensation changes (Phases 5–7).** Keyset pagination met its target (p95 42 ms at 10k). Future-dated raises are switched on by the daily job, with a cheap check on read as a safety net.
- **Departments, titles, levels (Phase 7A).** I asked why there was no table for role, since free text contradicts the requirement about inconsistent names. They became lookup tables, with levels ranked so `L10` sorts after `L2`.
- **Exchange rates, import, export (Phases 8–11).** The daily job refetches after one hour so a late run can't leave rates a day stale. CSV import validates again on confirm. The export reuses the directory's own query, and 13 tests compare the two.
- **Analytics and frontend (Phases 12–13).** Views are computed in SQL and checked against separately worked pure-Python definitions. A real browser run and an accessibility audit found rendering and contrast bugs, which were fixed.
- **Hardening and deployment (Phases 14–15).** Hardening found that `TRUNCATE ... CASCADE` could wipe history, fixed in migration `0012`. The full CSV export measured 639 ms (p95), over 500 ms, and is recorded as outside the requirement. The agent environment had no Docker, so the image was not built there and deployment is still to do.
- **Scale Lab round 2 (Phase 16).** Several results went against the hypotheses (no indexes was 4.4× slower to load; staging import only 1.8–2.5× faster). The trigram index looked useless until the cause was found: the driver's prepared statements switched Postgres to a plan that ignores it.

---

## Entry template

**N. Short title.** What I asked or noticed, what the AI produced, what I changed or decided, and why.
