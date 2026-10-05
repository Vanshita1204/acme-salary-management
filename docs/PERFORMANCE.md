# Performance — Scale Lab Results

Results of the Phase 1 Scale Lab experiments (`docs/SCALE_LAB.md`, E1–E3), run at **S = 10,000** and **M = 1,000,000** employees. Raw numbers, timings and full `EXPLAIN (ANALYZE, BUFFERS)` plans are in `backend/scale_lab/results/*.json`; `python -m scale_lab.report` regenerates the tables below from them.

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
