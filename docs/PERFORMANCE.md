# Performance — Scale Lab Results

Results of the Scale Lab experiments (`docs/SCALE_LAB.md`): E1–E3 in Phase 1, E4–E8 in Phase 16 (further down, with their own machine note). Run at **S = 10,000** and **M = 1,000,000** employees. Raw numbers, timings and full `EXPLAIN (ANALYZE, BUFFERS)` plans are in `backend/scale_lab/results/*.json`; `python -m scale_lab.report` regenerates the tables below from them.

All results are synthetic data on one local machine. They show how queries and the data model behave at volume, not production traffic or concurrency.

## Setup
- **Machine:** Apple Silicon Mac (arm64), 10 CPU cores, 16 GB RAM, internal SSD.
- **Database:** PostgreSQL 16.15 in Docker (docker-compose), default configuration: `shared_buffers` 160 MB, `work_mem` 4 MB, `effective_cache_size` 5 GB, 2 parallel workers per gather.
- **Data:** `python -m app.seed.employees --count N --as-of 2026-10-01`, fixed seed 1204, so every run is identical. The seed generates a full history, so there are ~18 compensation records per employee rather than the ~3 `SCALE_LAB.md` assumed.

| Dataset | Employees | Compensation records | Current-compensation rows | On disk (records) |
|---|---|---|---|---|
| S | 10,000 | 178,527 | 43,493 | 30 MB |
| M | 1,000,000 | 17,763,707 | 4,341,442 | 2.9 GB |

- **Method:** `ANALYZE` after loading; each query run once to warm up, then 5 times; the median is reported.
- **Harness:** `backend/scale_lab/` (`lab.py`, `e1_loading.py`, `e2_pagination.py`, `e3_current_compensation.py`). Each experiment runs on its own lab database (`acme_lab_*`), never the dev database.

---

## E1 — Bulk loading

**Hypothesis:** `COPY` beats batched `INSERT`, which beats ORM and row-by-row inserts, by roughly an order of magnitude end to end.

| Employees | Method | Seconds | Rows/s |
|---|---|---|---|
| 10,000 | row-by-row `INSERT` | 71.5 | 2,635 |
| 10,000 | ORM unit of work | 26.2 | 7,195 |
| 10,000 | batched `INSERT` (multi-row VALUES) | 15.1 | 12,500 |
| 10,000 | `COPY` | 9.1 | 20,657 |
| 1,000,000 | `COPY` | 1,000.6 | 18,752 |
| 1,000,000 | batched `INSERT` | 1,620.4* | 11,580* |

"Rows" = employees + compensation records. Times are end to end, including generating the data in Python, which takes only 1.3 s per 10k employees.
- **Identical output:** a checksum over the loaded data matched across all four methods at 10k, and between `COPY` and batched `INSERT` at 1M (17,763,707 records each; a sum of per-record hashes, since concatenating 17.7M rows exceeds Postgres's 1 GB string limit).
- \* **Run noise:** the 1M batched run was slowed for ~11 minutes by other load on the machine (load average ~6). The seed process stayed CPU-bound and Postgres was idle between batches, so the cost is client-side SQL building. Outside that window it loaded ~40k employees/min, which would put the clean total nearer ~1,450 s. The ratio to `COPY` (~1.5–1.6×) matches 10k either way.
- **Not run at 1M:** row-by-row and ORM. They scale linearly, so the 10k numbers put them at ~2 h and ~45 min.

**Result:** the hypothesis holds in order, but the spread is about 8×, not 10× or more. `COPY` is 1.6–1.7× faster than batched `INSERT` (at both sizes) and 7.9× faster than row-by-row. Throughput barely drops from 10k to 1M (20.7k → 18.8k rows/s), so loading scales linearly.

The gap is smaller than expected. The likely reason is that every `compensation_records` row still passes through the validation trigger (hire-date, currency and base-pay checks, two lookups per row), whichever loading method is used — a per-row floor no loading method avoids. That wasn't measured separately here; loading with triggers and indexes disabled is Scale Lab E7.

**Decision:** the seed's default method is now `COPY` (`--method copy`). The other methods stay available for comparison.

**Bug found:** the first 1M load failed at employee 1,000,000 — `duplicate key value violates unique constraint "employees_code_key"`.
- **Cause:** employee codes were generated with `lpad(n, 6, '0')`, and Postgres's `lpad` *truncates* longer strings, so hire #1,000,000 became `EMP-100000` and collided with hire #100,000.
- **Fix:** fixed in migration `0006` with an `employee_code()` function that pads to at least 6 digits and never truncates. Covered by a boundary test.
- **Significance:** the app itself would have failed on its millionth hire. This was the most valuable result of the lab.

---

## E2 — Pagination

**Hypothesis:** `OFFSET` gets linearly slower with page depth; keyset (cursor) pagination stays flat.

| Employees | Sort | Method | First page | Middle page | Last page |
|---|---|---|---|---|---|
| 10,000 | hire date | `OFFSET` | 0.46 ms | 1.77 ms | 3.11 ms |
| 10,000 | hire date | keyset | 0.38 ms | 0.40 ms | 0.39 ms |
| 10,000 | name | `OFFSET` | 0.46 ms | 5.43 ms | 10.25 ms |
| 10,000 | name | keyset | 0.41 ms | 0.51 ms | 0.49 ms |
| 1,000,000 | hire date | `OFFSET` | 0.48 ms | 864 ms | 1,641 ms |
| 1,000,000 | hire date | keyset | 0.41 ms | 0.70 ms | 0.61 ms |
| 1,000,000 | name | `OFFSET` | 0.49 ms | 1,325 ms | 2,603 ms |
| 1,000,000 | name | keyset | 0.47 ms | 0.96 ms | 0.59 ms |
| 1,000,000 | name (+`id` in index) | `OFFSET` | 0.55 ms | 1,027 ms | 1,986 ms |
| 1,000,000 | name (+`id` in index) | keyset | 0.51 ms | 0.51 ms | 0.48 ms |

Page of 50. Keyset queries use a row-value comparison on the full sort key, ties broken by `id`.

**Result: confirmed.**
- `OFFSET` has to read and discard every row before the page, so its cost grows with depth: invisible at 10k (10 ms), unusable at 1M (2.6 s for the last page by name).
- Keyset stays under 1 ms at every position, at both sizes.
- **Tie-breaker in the index:** adding `id` to the name index (`lower(last_name), lower(first_name), id`) makes keyset slightly steadier at 1M (0.96 → 0.51 ms mid-page). Without it, the planner sorts ties after the index scan. That's a small gain for an extra index; not needed at 10k.

**Decision:** keyset pagination, as specified in FR-1. Built into Phase 5's `GET /employees`.

---

## E3 — Current compensation per employee

**Hypothesis:** deriving current compensation from history (`DISTINCT ON` or `ROW_NUMBER()`) is fine for a page of employees but too slow for whole-population work. The denormalized `current_compensation` table fixes the latter.

Three workloads, each answered three ways:
- **page:** 50 employees by name with their total compensation (CTC).
- **aggregate:** total CTC per country across active employees.
- **top:** the 50 highest-paid employees, i.e. the directory sorted by compensation.

The two history-based strategies were run with and without the composite index `(employee_id, compensation_type_id, effective_date DESC)` on `compensation_records`.

| Employees | Workload | `DISTINCT ON` | `ROW_NUMBER()` | `current_compensation` table | `DISTINCT ON`, no index | `ROW_NUMBER()`, no index |
|---|---|---|---|---|---|---|
| 10,000 | page | 1.9 ms | 2.1 ms | 1.4 ms | 14 ms | 14 ms |
| 10,000 | aggregate | 146 ms | 132 ms | 61 ms | 176 ms | 148 ms |
| 10,000 | top | 70 ms | 91 ms | 22 ms | 102 ms | 124 ms |
| 1,000,000 | page | 1.8 ms | 1.7 ms | 1.8 ms | 2,935 ms | 2,497 ms |
| 1,000,000 | aggregate | 18,764 ms | 16,182 ms | 4,165 ms | 16,734 ms | 14,358 ms |
| 1,000,000 | top | 9,229 ms | 10,700 ms | 2,890 ms | 7,829 ms | 10,379 ms |

**Result: confirmed, with one limit found.**
- **Page:** all three strategies are equally fast (~2 ms) at both sizes, **as long as the composite index exists**. Without it, one page at 1M takes 2.5–2.9 s, because history is scanned instead of looked up. The index is essential for every per-employee history read (profile and history views, Phase 6).
- **Whole population:** the `current_compensation` table is 3–4× faster than deriving from history (4.2 s vs 16–19 s for the aggregate at 1M). The composite index doesn't help here: these queries read all of history either way, so the no-index runs are slightly faster (no index overhead). This is exactly the case the denormalized table exists for.
- **Limit:** even with the table, whole-population queries at 1M take seconds — 4.2 s for the aggregate, 2.9 s for top-by-compensation. The table removes the "latest record" search but still has to aggregate ~4.3M current rows and join employees.
  - At 10k (the deployed size) these take 22–61 ms, well within target.
  - At 1M, the directory's compensation sort and the Phase 12 analytics need a further step: a per-employee `employee_totals` row with an indexed USD total (the fix already noted as the open trade-off in `DATABASE_DESIGN.md`), and/or materialized aggregates (Scale Lab E5).

**Decisions:**
- Keep the `current_compensation` table as the source for directory totals and analytics (Phase 5 already reads it).
- Keep the composite index on `compensation_records`.
- Defer `employee_totals` / materialized analytics until the target size exceeds the deployed 10k. At 1M, the compensation sort (2.9 s) and country aggregates (4.2 s) would miss the 500 ms target without them.

---

## Directory endpoint (Phase 5 acceptance)

`python -m scale_lab.directory_latency`: 15 search/filter/sort combinations, alternating first and second pages, through the full FastAPI stack. 20 requests per combination at 10k; 10 at 1M.

| Request type | 10k median | 1M median |
|---|---|---|
| Sort by name or hire date, with or without filters | 6–8 ms | 7–41 ms |
| Search that matches early (`q=an`) | 7 ms | 8 ms |
| Search that must scan most rows (rare surname, employee code, no match) | 11–28 ms | **774–1,324 ms** |
| Sort by compensation | 32–42 ms | **2,682–3,576 ms** |
| **Overall p95** | **42 ms** | **3,499 ms** |

**Result:**
- **At 10k (the deployed size):** the endpoint meets its target with a wide margin — p95 42 ms vs < 500 ms.
- **At 1M:** everything index-backed stays fast, and the two slow cases are exactly the ones the lab predicted:
  - The compensation sort aggregates every matching employee's current compensation (E3's limit).
  - `ILIKE '%term%'` search can't use a B-tree index, so a selective term scans all employees. That's Scale Lab E4 (`pg_trgm`).
- Neither needs fixing for the 10k submission; both are the first changes needed to scale past it.

**Re-measured after Phase 7A** (department/title/level became FK lookups; the mix gained two level-sort/rank-filter requests, 17 combinations). Run on a different, slower machine than the table above, so both versions were measured there on the same 10k data:

| | p95 (same machine) |
|---|---|
| Before 7A (`directory_latency_phase7a_baseline_10k.json`) | 65 ms |
| After 7A (`directory_latency_phase7a_10k.json`) | 70–73 ms |

About 5–10% slower — the employee rows now join three tiny lookup tables for their names — and still far under the 500 ms target. The new sort by level and rank-range filter run at 22–26 ms median.

---

## Final check — Phase 14

Re-run on the final schema (all migrations through `0012`), pristine 10,000 employees, with scratch exchange rates inserted for the run and deleted afterwards (`scale_lab/api_latency.py`; results in `scale_lab/results/*phase14_10k.json`).

| Request mix | Requests | p95 | Target |
|---|---|---|---|
| Directory: 11 filter / sort / search combinations | list, search, filter | **80 ms** | < 500 ms |
| Analytics: all seven views, 10 combinations | | **391 ms** (slowest view, year-over-year change: 453 ms) | < 500 ms |
| CSV export, full unfiltered | file download | **639 ms** (median ~538 ms) | not covered |
| Phase 5 benchmark, same as above | | 66.9 ms | no regression (65 ms before 7A, 70–73 ms after) |

The export is over 500 ms, but the requirement covers listing, searching and filtering, not downloading a file of every employee. About 250 ms goes to loading ORM entities, the rest to totals batches and 73 ms of CSV writing; no cheap fix, and filtered exports are far faster.

---

## Phase 2 experiments (E4–E8)

**Machine for this section:** a different one from the table above — Linux x86-64, 4 vCPU, 16 GB RAM, PostgreSQL 16.14 (distribution package, same settings: `shared_buffers` 160 MB, `work_mem` 4 MB). So absolute times here are not comparable with E1–E3; compare columns within a table. The datasets are the same (S, M; M took 1,141 s to seed here vs 1,001 s on the Mac). **The plan's L dataset (10 M employees, ~180 M records at this seed's ~18 records per employee) was not run**: it needs well over 100 GB of disk and many hours to load. E8, the only experiment defined on L, was run on M instead. Raw results: `backend/scale_lab/results/e4_…` to `e8_…`; `python -m scale_lab.report` regenerates the tables.

### E4 — Search

**Hypothesis:** a B-tree can only serve a prefix, so it can't replace the directory's substring search over four expressions (full name, last name, email, code); one `pg_trgm` GIN index per expression can, for terms of 3+ characters. Short terms have no trigram and should stay a scan.

Median ms, the directory's real page query (search, name order, 51 rows):

| Term | Matches | No index | Trigram GIN |
|---|---|---|---|
| 2 characters | 310,974 | 1.41 | 1.45 |
| 3 characters, common | 58,637 | 1.92 | 2.49 |
| surname | 218 | 1.74 | 1.74 |
| part of a full name | 3 | 1,983 | 6.08 |
| part of a code | 100 | 2,033 | 12.0 |
| part of an email | 1 | 2,079 | 1.41 |
| no match | 0 | 3,857 | 0.44 |

At S (10,000) the same rare terms take 15–26 ms unindexed and ~1 ms with the index.

| Last-name prefix only (a different, narrower question) | No index | B-tree prefix |
|---|---|---|
| `ab…` | 150 | 2.45 |
| `abbott…` | 132 | 1.33 |

- **Cost:** four trigram indexes 161 MB (the employees table is 406 MB) and 17 s to build; the B-tree prefix index 7 MB and 1.6 s. Results were identical with and without the index for every term.
- **Plan:** unindexed, Postgres walks the name-order index and filters until it has 51 rows. That is instant when many people match (the first three rows) and a full pass when few do. With the trigram indexes it does a `BitmapOr` over the four, then a small sort.
- **Hypothesis confirmed, with two corrections.** (1) Short or common terms never needed the index, because the ordered scan stops early; the case that stays slow is a *short term few people match*, which was not measured. (2) **The index alone is not enough.** The driver (psycopg 3, which the app uses through SQLAlchemy) prepares a statement after 5 executions, and Postgres can then switch to one generic plan that cannot know the term is rare. On one connection the same search went from ~2 ms to ~4,000 ms after about ten executions; measured at steady state with the default driver, "part of a full name" took 3,781 ms even with the indexes in place, while the same query with a per-term plan took 6 ms. Searching at 1M therefore needs the indexes **and** per-term plans (`prepare_threshold=None` on the connection, or `plan_cache_mode = force_custom_plan`). At 10,000 neither matters, so the application was not changed.
- A trigram index also adds write cost (E7 measured the existing indexes only; the four trigram indexes were not part of it).

### E5 — Analytics aggregation

**Hypothesis:** the live views cost a pass over every active employee's current compensation, so they grow linearly (fine at 10k, seconds at 1M); a materialized view with one row per active employee (annual total in USD plus the filter dimensions) turns each into a scan of one narrow table, several times faster, at the price of staleness and a refresh that rewrites everything.

Median ms, M (S in the last column):

| View | Live | Materialized | Faster | Live at S |
|---|---|---|---|---|
| Summary (count, spend, average, median) | 6,178 | 563 | 11× | 52 |
| Stats by department | 6,949 | 910 | 7.6× | 42 |
| Stats by department, one country | 1,234 | 273 | 4.5× | 28 |
| One title across countries | 592 | 101 | 5.8× | 10 |

- The view has 862,450 rows (active employees with a stored rate), 87 MB, and builds in 4 s. Live and materialized answers were identical.
- **Refresh costs 5.3–5.5 s whatever changed** (100, 1,000 or 10,000 employees), and 8.4–8.9 s with `CONCURRENTLY` (which keeps the view readable and needs the unique index). At S it is 0.05–0.3 s.
- **Confirmed**, with limits. 4.5–11× faster, but at 1M the two whole-population views still take 0.56–0.91 s because the median over 862k rows is itself the cost, so a view alone does not reach 500 ms; per-dimension summary tables would. The view stores USD, so a daily rate refresh also makes it stale. At the deployed 10k the live views take 10–52 ms and the Phase 14 check (p95 391 ms for the whole mix) stands, so this is not needed there.

### E6 — Large CSV import

**Hypothesis:** the application parses, validates and builds every row in Python, so time and memory grow linearly (the 10,000-row cap exists for that reason). Streaming the file with `COPY` into a staging table, validating with set-based SQL and inserting with three `INSERT … SELECT` statements keeps the process flat and should be several times faster, with the database's per-row triggers as the floor.

Both paths run the full job (stage/parse, validate, insert employees, base-pay records and current compensation) in one transaction, then roll back so the dataset is unchanged. Each ran in its own process. Employees already present: 1,000,000.

| Rows | File | Path | Seconds | Peak process memory |
|---|---|---|---|---|
| 10,000 | 1.1 MB | application | 4.5 | 120 MB |
| 10,000 | 1.1 MB | staging | 2.6 | 57 MB |
| 100,000 | 11.5 MB | staging | 16.1 | 64 MB |
| 1,000,000 | 118 MB | staging | 155 | 118 MB |

- At S (10,000 employees present) the application takes 3.8 s and staging 1.5 s for 10,000 rows. The application was not run above its cap; its memory grows ~3.8 KB per row (86 MB at 1,000 rows, 120 MB at 10,000), which would be several GB at 1M (extrapolated, not measured).
- At 1M rows: validation 11.8 s, employees 72 s, records 51 s, current compensation 17.5 s. **The inserts dominate**: that is the employee table's indexes and the per-row record trigger, not Python.
- A file with 3 bad rows among 1,000,000 was rejected after 14.8 s with the three problems reported and nothing written.
- **Partly confirmed.** Memory is flat (57 → 118 MB across 100× more rows) and time is linear (~1.6 ms per row). But the speed-up is **1.8× at M and 2.5× at S**, not "several times": at 10,000 rows the database work is what the application path also waits for.
- **Two things that mattered more than the design.** (1) A temporary table is never analyzed: without `ANALYZE import_staging` the planner picked nested loops and validation took 10.4 s at 100k rows and 95 s at 1M (1.8 s and 11.8 s with it). (2) `work_mem = 256MB` made no difference at 100k and made 1M validation **8× slower** (95.7 s); the plan changed and the cause was not investigated.
- **Difference to weigh before adopting it:** the SQL email check is a pattern, not the full `email-validator` rules the application applies, and server-side memory was not measured.

### E7 — Index write cost

**Hypothesis:** each secondary index adds work to every insert, so a load with only constraint-backing indexes is clearly faster than one with all of them; reads are the reverse, and an "essential" subset should capture most of the read benefit at a fraction of the write cost.

Three fresh databases, identical seed, 100,000 employees (~1.8 M records). *Essential* = the history lookup, the partial future-dated index and the name-sort index; *all* = the 11 secondary indexes the migrations create.

| | none | essential | all |
|---|---|---|---|
| Secondary indexes | 0 | 3 | 11 |
| Index size | 72 MB | 145 MB | 156 MB |
| Bulk load | **432.7 s** | 98.1 s | 101.8 s |
| Single record insert (ms) | 0.86 | 0.94 | 0.85 |
| Single employee insert (ms) | 0.87 | 1.13 | 0.79 |
| Department + country page (ms) | 16.2 | 0.90 | 1.33 |
| Name page, deep (ms) | 32.6 | 0.45 | 0.51 |
| Hire-date page (ms) | 13.2 | 14.4 | 0.25 |
| One employee's history (ms) | 55.9 | 0.26 | 0.35 |
| Future-dated records (ms) | 275 | 0.18 | 0.21 |
| Active employees at one level (count, ms) | 14.8 | 13.3 | 10.5 |

- **Disproven in part.** Zero indexes was not faster to load: it was 4.4× *slower*, because the seed reads back the records it just wrote to fill current compensation, and without the history index every batch scans the growing table. Going from essential to all added 8 indexes for **+3.7% load time** and +11 MB; single-row insert latency differences (0.79–1.13 ms) were inside the noise and did not follow the index count.
- The read side is as predicted: the essential set gives most of the gain; only the hire-date page needs one of the remaining indexes (14 ms → 0.25 ms). The level count is 10–15 ms in all three, so those indexes did little for it.
- **Conclusion:** keep all 11 indexes. At this size their write cost is small next to the reads they make instant. Single-row inserts here are dominated by the commit (~0.8 ms) on a local disk, which can hide per-index cost; the bulk-load difference is the cleaner measure.

### E8 — Partitioning (run on M, not L)

**Hypothesis:** a date-range report scans every record without partitioning and only the matching partition with it, so it is several times faster and no worse than a date index; the cost is on lookups that don't filter by date and on the schema (`id` can no longer be unique on its own).

M, 17.76 M records, yearly partitions (16 + default). Three copies with the same columns, no triggers or foreign keys, so only the layout differs. Median ms:

| Query | Plain | Plain + date index | Partitioned |
|---|---|---|---|
| Records in one quarter (count, sum) | 486 | 966 | **130** |
| One year grouped by reason | 682 | 637 | **285** |
| One employee's history | 0.33 | 0.18 | 1.14 |
| One record by id | 0.70 | 0.48 | 0.56 |
| Current per type, 50 employees | 1.45 | 0.90 | 2.29 |
| Single-row insert | 0.61 | | 0.82 |

- Size: 3.06 GB plain, 3.22 GB partitioned (+5%). Pruning worked: the quarter query scanned one partition.
- A date index did not help the quarter query (it got slower: a quarter is a large part of the table, so the index scan does random heap reads).
- **Structural limit, shown by trying it:** `PRIMARY KEY (id)` or `UNIQUE (id)` on the partitioned table fails ("unique constraint on partitioned table must include all partitioning columns"). The key becomes `(id, effective_date)`, and `current_compensation.compensation_record_id`, which is unique and references a record's `id`, could not point at it without also storing the date.
- **Confirmed for date-range reports (2.2–3.7×), at a real cost:** per-employee lookups 1.6–3.5× slower (they probe every partition) and the schema change above. **Not worth doing** while the reports in the requirements (change report, one year) run in under a second at M; revisit only if date-range scans over a much larger table become the bottleneck. The absolute numbers are M's; L was not measured.

---

## Summary — what the lab confirmed or changed

| Decision | Status | Evidence |
|---|---|---|
| Keyset pagination for the directory (FR-1) | Confirmed | E2: `OFFSET` 2.6 s at 1M vs keyset < 1 ms |
| Denormalized `current_compensation` table | Confirmed | E3: 3–4× faster for whole-population queries; equal for pages |
| Composite index on `compensation_records` (employee, type, date) | Confirmed, essential | E3: page 1.8 ms with it, 2.9 s without, at 1M |
| Seed loading method | **Changed** to `COPY` | E1: 1.7× faster than batched `INSERT`, linear to 1M |
| Employee code generation | **Fixed** (bug) | E1 at 1M: `lpad` truncation collided codes at hire #1,000,000 |
| Directory sort by compensation / live analytics at 1M | Limit recorded | E3 and directory latency: 2.7–4.2 s at 1M even with the table → `employee_totals` / E5 before scaling past 10k |
| Directory free-text search at 1M | Limit recorded | Directory latency: 0.8–1.3 s for selective terms (full scan) → `pg_trgm` index (E4) before scaling past 10k |
| `id` tie-breaker in the name index | Optional | E2: steadier keyset at 1M (0.96 → 0.51 ms); not needed at 10k |
| Directory free-text search at 1M | Fix measured | E4: trigram GIN takes rare terms from 2–4 s to 0.4–12 ms; needs per-term plans (driver prepared statements flip it back to 4 s) |
| Live analytics at 1M | Fix measured | E5: materialized view 4.5–11× faster, refresh 5.4 s regardless of changes; still 0.6–0.9 s for global views |
| In-memory CSV import with a 10,000-row cap | Confirmed as a cap; alternative measured | E6: staging + SQL scales linearly with flat memory (1M rows in 155 s, 118 MB), only 1.8–2.5× faster; inserts dominate |
| The 11 secondary indexes | Confirmed | E7: all vs essential +3.7% load time; hire-date index turns 14 ms into 0.25 ms; none at all = 4.4× slower load |
| Partition compensation records by date | Rejected for now | E8: date reports 2.2–3.7× faster, employee lookups 1.6–3.5× slower, `id` can't stay unique |
