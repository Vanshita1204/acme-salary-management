# Performance — Scale Lab Results

Results of the Scale Lab experiments (`docs/SCALE_LAB.md`): E1–E3 in Phase 1, E4–E8 in Phase 16. Run at **S = 10,000** and **M = 1,000,000** employees. Raw numbers and full `EXPLAIN (ANALYZE, BUFFERS)` plans are in `backend/scale_lab/results/*.json`; `python -m scale_lab.report` regenerates the tables.

All results are synthetic data on one local machine. They show how queries and the data model behave at volume, not production traffic or concurrency.

## Setup

- **Machine (E1–E3, directory and Phase 14 checks):** Apple Silicon Mac, 10 cores, 16 GB RAM, SSD.
- **Machine (E4–E8):** Linux x86-64, 4 vCPU, 16 GB RAM. Absolute times are not comparable with E1–E3; compare columns within a table.
- **Database:** PostgreSQL 16 in Docker, default configuration (`shared_buffers` 160 MB, `work_mem` 4 MB).
- **Data:** `python -m app.seed.employees --count N --as-of 2026-10-01`, fixed seed 1204. The seed generates full history, so ~18 compensation records per employee.

| Dataset | Employees | Compensation records | Current-compensation rows | On disk (records) |
| --- | --- | --- | --- | --- |
| S | 10,000 | 178,527 | 43,493 | 30 MB |
| M | 1,000,000 | 17,763,707 | 4,3 41,442 | 2.9 GB |

- **Method:** `ANALYZE` after loading; each query run once to warm up, then 5 times; the median is reported.
- **Harness:** `backend/scale_lab/`. Each experiment runs on its own lab database, never the dev database.
- **Not run:** the plan's L dataset (10 M employees) needs over 100 GB and many hours to load. E8, the only experiment defined on L, was run on M.

---

## E1 — Bulk loading

**Hypothesis:** `COPY` beats batched `INSERT`, which beats ORM and row-by-row inserts, by roughly an order of magnitude.

| Employees | Method | Seconds | Rows/s |
| --- | --- | --- | --- |
| 10,000 | row-by-row `INSERT` | 71.5 | 2,635 |
| 10,000 | ORM unit of work | 26.2 | 7,195 |
| 10,000 | batched `INSERT` | 15.1 | 12,500 |
| 10,000 | `COPY` | 9.1 | 20,657 |
| 1,000,000 | `COPY` | 1,000.6 | 18,752 |
| 1,000,000 | batched `INSERT` | 1,620.4* | 11,580* |

"Rows" = employees + compensation records; times include generating the data. A checksum confirmed identical output across methods. \*The 1M batched run was slowed by other load on the machine; the ratio to `COPY` (~1.5–1.6×) matches 10k.

**Result:** order confirmed, but the spread is ~8×, not 10×. `COPY` is 1.6–1.7× faster than batched `INSERT` and 7.9× faster than row-by-row. Throughput barely drops from 10k to 1M, so loading scales linearly. The likely floor is the per-row validation trigger on `compensation_records`.

**Decision:** the seed's default method is `COPY` (`--method copy`).

**Bug found:** the first 1M load failed at employee 1,000,000 with `duplicate key ... "employees_code_key"`. Codes were generated with `lpad(n, 6, '0')`, which *truncates* longer strings, so hire #1,000,000 became `EMP-100000` and collided with #100,000. Fixed in migration `0006` with an `employee_code()` function that never truncates, covered by a boundary test. The app would have failed on its millionth hire.

---

## E2 — Pagination

**Hypothesis:** `OFFSET` gets linearly slower with page depth; keyset pagination stays flat.

Page of 50, median:

| Employees | Sort | Method | First page | Middle page | Last page |
| --- | --- | --- | --- | --- | --- |
| 10,000 | name | `OFFSET` | 0.46 ms | 5.43 ms | 10.25 ms |
| 10,000 | name | keyset | 0.41 ms | 0.51 ms | 0.49 ms |
| 1,000,000 | hire date | `OFFSET` | 0.48 ms | 864 ms | 1,641 ms |
| 1,000,000 | hire date | keyset | 0.41 ms | 0.70 ms | 0.61 ms |
| 1,000,000 | name | `OFFSET` | 0.49 ms | 1,325 ms | 2,603 ms |
| 1,000,000 | name | keyset | 0.47 ms | 0.96 ms | 0.59 ms |

**Result: confirmed.** `OFFSET` reads and discards every earlier row, so it is unusable at 1M (2.6 s for the last page by name). Keyset stays under 1 ms everywhere. Adding `id` to the name index steadies keyset slightly at 1M (0.96 → 0.51 ms) but isn't needed at 10k.

**Decision:** keyset pagination (FR-1), built into `GET /employees`.

---

## E3 — Current compensation per employee

**Hypothesis:** deriving current compensation from history (`DISTINCT ON` / `ROW_NUMBER()`) is fine for a page but too slow for whole-population work; the denormalized `current_compensation` table fixes the latter.

Workloads: **page** (50 employees with total compensation), **aggregate** (total per country, active employees), **top** (50 highest-paid). Median:

| Employees | Workload | `DISTINCT ON` | `ROW_NUMBER()` | `current_compensation` table | `DISTINCT ON`, no index |
| --- | --- | --- | --- | --- | --- |
| 10,000 | page | 1.9 ms | 2.1 ms | 1.4 ms | 14 ms |
| 10,000 | aggregate | 146 ms | 132 ms | 61 ms | 176 ms |
| 10,000 | top | 70 ms | 91 ms | 22 ms | 102 ms |
| 1,000,000 | page | 1.8 ms | 1.7 ms | 1.8 ms | 2,935 ms |
| 1,000,000 | aggregate | 18,764 ms | 16,182 ms | 4,165 ms | 16,734 ms |
| 1,000,000 | top | 9,229 ms | 10,700 ms | 2,890 ms | 7,829 ms |

"No index" = without the composite index `(employee_id, compensation_type_id, effective_date DESC)`.

**Result:** confirmed, with one limit.

- **Page:** all strategies take ~2 ms *if the composite index exists*; without it, a page at 1M takes ~2.5–2.9 s. The index is essential for every per-employee history read.
- **Whole population:** the table is 3–4× faster than deriving from history (4.2 s vs 16–19 s for the aggregate at 1M).
- **Limit:** even with the table, 1M whole-population queries take seconds (aggregate 4.2 s, top 2.9 s). At 10k they take 22–61 ms, well within target.

**Decisions:** keep the `current_compensation` table and the composite index. Defer `employee_totals` (an indexed per-employee USD total) and materialized analytics until the target size exceeds 10k.

---

## Directory endpoint (Phase 5 acceptance)

`python -m scale_lab.directory_latency`: 15 search/filter/sort combinations through the full FastAPI stack.

| Request type | 10k median | 1M median |
| --- | --- | --- |
| Sort by name or hire date, with or without filters | 6–8 ms | 7–41 ms |
| Search that matches early | 7 ms | 8 ms |
| Search that must scan most rows (rare term, no match) | 11–28 ms | **774–1,324 ms** |
| Sort by compensation | 32–42 ms | **2,682–3,576 ms** |
| **Overall p95** | **42 ms** | **3,499 ms** |

At 10k (the deployed size) the endpoint meets the < 500 ms target with wide margin. At 1M, index-backed requests stay fast; the two slow cases are the compensation sort (E3's limit) and `ILIKE '%term%'` search, which a B-tree can't serve (addressed by E4).

**After Phase 7A** (department/title/level became FK lookups), on a slower machine with both versions measured on the same 10k data: p95 65 ms before, 70–73 ms after. About 5–10% slower from three small joins, still far under target.

---

## Final check — Phase 14

Re-run on the final schema (migrations through `0012`), 10,000 employees (`scale_lab/api_latency.py`).

| Request mix | p95 | Target |
| --- | --- | --- |
| Directory: 11 filter / sort / search combinations | **80 ms** | < 500 ms |
| Analytics: all seven views, 10 combinations | **391 ms** (slowest: year-over-year change, 453 ms) | < 500 ms |
| CSV export, full unfiltered | **639 ms** | not covered |
| Phase 5 benchmark | 66.9 ms | no regression |

The export exceeds 500 ms, but the requirement covers listing, searching and filtering, not downloading every employee. Filtered exports are far faster.

---

## E4 — Search

**Hypothesis:** a B-tree can't serve the directory's substring search over four expressions (full name, last name, email, code); one `pg_trgm` GIN index per expression can, for terms of 3+ characters.

Median ms at M, the directory's real page query:

| Term | Matches | No index | Trigram GIN |
| --- | --- | --- | --- |
| 2 characters | 310,974 | 1.41 | 1.45 |
| 3 characters, common | 58,637 | 1.92 | 2.49 |
| surname | 218 | 1.74 | 1.74 |
| part of a full name | 3 | 1,983 | 6.08 |
| part of a code | 100 | 2,033 | 12.0 |
| part of an email | 1 | 2,079 | 1.41 |
| no match | 0 | 3,857 | 0.44 |

At S, rare terms take 15–26 ms unindexed and ~1 ms indexed. The four trigram indexes total 161 MB (17 s to build). Results were identical with and without them.

**Result:** confirmed, with two corrections.

1. Short or common terms never needed the index, because the ordered scan stops early. A *short term few people match* stays slow and wasn't measured.
2. **The index alone is not enough.** psycopg 3 prepares a statement after 5 executions, and Postgres can switch to a generic plan that can't know the term is rare. The same search went from ~2 ms to ~4,000 ms after about ten executions. Searching at 1M needs the indexes **and** per-term plans (`prepare_threshold=None` or `plan_cache_mode = force_custom_plan`).

At 10k neither matters, so the application was not changed.

---

## E5 — Analytics aggregation

**Hypothesis:** live views cost a pass over every active employee's current compensation; a materialized view with one row per active employee should be several times faster, at the price of staleness and a full-rewrite refresh.

Median ms:

| View | Live (M) | Materialized (M) | Faster | Live (S) |
| --- | --- | --- | --- | --- |
| Summary (count, spend, average, median) | 6,178 | 563 | 11× | 52 |
| Stats by department | 6,949 | 910 | 7.6× | 42 |
| Stats by department, one country | 1,234 | 273 | 4.5× | 28 |
| One title across countries | 592 | 101 | 5.8× | 10 |

- The view has 862,450 rows (87 MB) and builds in 4 s. Answers matched the live queries.
- Refresh takes 5.3–5.5 s regardless of how much changed (8.4–8.9 s with `CONCURRENTLY`).
- **Confirmed, with limits:** 4.5–11× faster, but the two whole-population views still take 0.56–0.91 s at M because the median over 862k rows is itself the cost. The view stores USD, so a daily rate refresh makes it stale. At 10k the live views take 10–52 ms, so it isn't needed there.

---

## E6 — Large CSV import

**Hypothesis:** the application parses and validates every row in Python (hence the 10,000-row cap). Streaming with `COPY` into a staging table, validating with set-based SQL and inserting with `INSERT … SELECT` keeps memory flat and should be several times faster.

Both paths run the full job in one transaction, then roll back. 1,000,000 employees already present.

| Rows | Path | Seconds | Peak memory |
| --- | --- | --- | --- |
| 10,000 | application | 4.5 | 120 MB |
| 10,000 | staging | 2.6 | 57 MB |
| 100,000 | staging | 16.1 | 64 MB |
| 1,000,000 | staging | 155 | 118 MB |

- At 1M rows the inserts dominate (employees 72 s, records 51 s), reflecting the table's indexes and per-row trigger, not Python.
- A 1M-row file with 3 bad rows was rejected after 14.8 s with nothing written.
- **Partly confirmed:** memory is flat and time is linear (~1.6 ms/row), but the speed-up is only **1.8× at M and 2.5× at S**.
- **Pitfall:** a temporary table is never analyzed. Without `ANALYZE import_staging`, validation took 95 s at 1M rows instead of 11.8 s. Raising `work_mem` to 256 MB made 1M validation 8× slower (cause not investigated).
- **Trade-off:** the SQL email check is a pattern, not the full `email-validator` rules the app applies.

---

## E7 — Index write cost

**Hypothesis:** each secondary index adds insert work, so a load with only constraint-backing indexes is clearly faster; an "essential" subset should capture most read benefit at a fraction of the write cost.

Three databases, 100,000 employees (~1.8 M records). *Essential* = history lookup, partial future-dated index, name-sort index; *all* = the 11 secondary indexes the migrations create.

| | none | essential | all |
| --- | --- | --- | --- |
| Secondary indexes | 0 | 3 | 11 |
| Index size | 72 MB | 145 MB | 156 MB |
| Bulk load | **432.7 s** | 98.1 s | 101.8 s |
| Department + country page (ms) | 16.2 | 0.90 | 1.33 |
| Name page, deep (ms) | 32.6 | 0.45 | 0.51 |
| Hire-date page (ms) | 13.2 | 14.4 | 0.25 |
| One employee's history (ms) | 55.9 | 0.26 | 0.35 |
| Future-dated records (ms) | 275 | 0.18 | 0.21 |

- **Partly disproven:** no indexes was 4.4× *slower* to load, because the seed reads back the records it just wrote and, without the history index, every batch scans the growing table. Essential → all added 8 indexes for only **+3.7% load time**.
- Only the hire-date page needs one of the non-essential indexes (14 ms → 0.25 ms).
- **Conclusion:** keep all 11 indexes. Their write cost is small next to the reads they make instant.

---

## E8 — Partitioning (run on M, not L)

**Hypothesis:** a date-range report scans every record without partitioning but only the matching partition with it, at the cost of lookups that don't filter by date and a schema change.

M, 17.76 M records, yearly partitions. Median ms:

| Query | Plain | Plain + date index | Partitioned |
| --- | --- | --- | --- |
| Records in one quarter (count, sum) | 486 | 966 | **130** |
| One year grouped by reason | 682 | 637 | **285** |
| One employee's history | 0.33 | 0.18 | 1.14 |
| Current per type, 50 employees | 1.45 | 0.90 | 2.29 |
| Single-row insert | 0.61 | | 0.82 |

- Size: 3.06 GB plain vs 3.22 GB partitioned. A date index didn't help the quarter query (random heap reads over a large fraction of the table).
- **Structural limit:** `PRIMARY KEY (id)` fails on a partitioned table; the key must become `(id, effective_date)`, and `current_compensation.compensation_record_id` could no longer reference a record by `id` alone.
- **Confirmed for date-range reports (2.2–3.7×), at a real cost:** per-employee lookups are 1.6–3.5× slower and the schema must change. **Rejected for now**; the requirement's reports run under a second at M.

---

## E9 — Concurrency and connection pooling

Run on a **new 1M-employee lab database** (`acme_lab_1m`: 1,000,000 employees, 17.7 M compensation records, built with `COPY` in 1,003 s on the current schema and the 8 Acme entities), on the Apple Silicon Mac used for E1–E3. The server is the real one (`uvicorn`, its own process), the response cache is **off**, and clients are closed-loop: N clients each send requests back to back. `python -m scale_lab.e9_concurrency` (`backend/scale_lab/e9_concurrency.py`).

**Hypotheses (written before running):**

1. **Directory requests** (index-backed, a few ms each at 1M) are limited by the Python process, not the database: one uvicorn worker, sync endpoints in a thread pool, one GIL. Throughput will plateau at a low client count, and latency will then grow in proportion to the clients (Little's law).
2. **Pool size is not the bottleneck.** The default pool serves 15 requests at once (5 + 10 overflow). Above 15 clients, requests wait for a connection, adding latency, but throughput stays flat; raising the pool size alone will not raise throughput.
3. **Pay-insight views** (seconds each at 1M) are limited by database CPU. Throughput will stay roughly flat as clients rise past the number of cores, and latency grows in proportion.
4. **More workers** will raise directory throughput, up to about the core count, and will not help the analytics views.

**Setup:** Postgres 16 in Docker (10 CPUs, 8 GB, `shared_buffers` 128 MB, `max_parallel_workers_per_gather` 2, `/dev/shm` 64 MB), uvicorn with 1 worker and SQLAlchemy's default pool (5 + 10) unless stated. Directory workload: first page, search, department + country filter, hire-date sort. Heavy workload: the pay-insight views and a directory page sorted by pay. Each level ran 15 s (directory) or 60 s (heavy) after a warm-up; one run per setting. Raw numbers: `scale_lab/results/e9_concurrency_*.json`.

**Baseline, directory workload** (1M employees, cache off):

| Clients | Requests/s | p50 | p95 | p99 | Errors |
| --- | --- | --- | --- | --- | --- |
| 1 | 3.2 | 202 ms | 680 ms | 711 ms | 0 |
| 5 | 11.3 | 244 ms | 1,101 ms | 2,021 ms | 0 |
| 10 | 13.0 | 425 ms | 2,452 ms | 3,309 ms | 0 |
| 20 | 14.0 | 830 ms | 4,255 ms | 4,761 ms | 0 |
| 50 | 13.4 | 3,111 ms | 6,183 ms | 7,436 ms | 0 |

Throughput stops rising at about **13 requests/s from 5–10 clients**; past that, latency grows in step with the number of clients (50 clients / 13.4 req/s = 3.7 s). A single client already sees 200 ms per request, not the few ms the page query alone costs: the "total matching" count (`SELECT count(*)` over 1M employees, a parallel index-only scan, **60 ms** alone) runs on every request.

**Baseline, heavy workload:**

| Clients | Requests/s | p50 | p95 | Errors |
| --- | --- | --- | --- | --- |
| 1 | 0.1 | 8.1 s | 42.3 s | 0 |
| 2 | 0.1 | 9.3 s | 38.1 s | 0 |
| 4 | 0.2 | 8.7 s | 41.6 s | 0 |
| 8 | 0.3 | 14.3 s | 61.4 s | 0 |
| 16 | 0.4 | 22.9 s | 68.5 s | 1 |

One client, per view: pay-sorted directory page 2.8 s, summary 8.6 s, stats by department 7.5 s, composition 7.5 s, histogram 10.2 s, **outliers 42.3 s**.

**Where the time goes.** During a 10-client directory run the Python server used **12.8% of one core** and the Postgres container **1,000% (all 10 cores)**.

**Directory at 50 clients, with the setting changed:**

| Setting | Requests/s (5 / 10 / 20 / 50 clients) | p95 at 50 | Errors at 50 |
| --- | --- | --- | --- |
| 1 worker, pool 5 + 10 (baseline) | 11.3 / 13.0 / 14.0 / 13.4 | 6.2 s | 0 |
| 4 workers (pool 5 + 10 each) | 13.8 / 12.8 / 13.1 / 12.7 to 13.5 | 11.1–12.3 s | 6–10 (HTTP 503) |
| 1 worker, pool 50 + 0 | n/a / 12.4 / 13.5 / 13.5 to 14.6 | 9.8–10.2 s | 8–15 (HTTP 503) |

(The 50-client cases were run twice; ranges show both runs.)

**Verdicts:**

1. **Refuted.** The directory is not limited by the Python process: the server used 13% of a core while the database used all ten. The plateau and the linear growth of latency are as predicted, but the cause is the database.
2. **Confirmed.** With 50 clients and a pool of 15, no request failed from waiting for a connection, and a pool of 50 gave no more throughput (12.4–14.6 req/s against 13.4).
3. **Partly confirmed.** The views do not run one at a time: throughput rose from 0.1 to 0.4 req/s as clients went from 1 to 16, so the database uses several cores for them, flattening at around 8 clients. Latency grows in proportion beyond that.
4. **Refuted for the directory.** Four workers gave the same ~13 req/s, because the limit is the database. (Not measured: the analytics workload with more workers; it is database-bound too.)

**Unexpected: errors at 50 clients.** Runs with 4 workers or a 50-connection pool returned HTTP 503 on 6–15 requests. Postgres's log shows `could not resize shared memory segment … No space left on device`: parallel queries (each Gather node uses dynamic shared memory) run out of the container's small `/dev/shm` when many run at once. More connections made it *worse* because more parallel queries ran at the same time. It is a Docker default (`/dev/shm` of 64 MB), not an application bug, but it means "more connections or workers" can turn slow into failing.

**Conclusions so far:**

- Adding workers or connections does nothing while the database is the bottleneck, and can cause errors. The fix has to reduce database work per request.
- The biggest per-request costs at 1M employees are the exact `count(*)` on every directory call and the whole-population analytics queries (outliers 42 s).
- The pool and worker count are worth tuning only after the queries are cheaper.

**Next:** E10, the cost of the exact total count on every directory request (options: cached counts, estimated counts, skipping the total when unfiltered); then the analytics queries (outliers first).

---

## E10 — The directory's total count

E9 showed the directory is limited by database CPU, and that every request runs an exact `count(*)` for the "N employees match" figure (60 ms alone at 1M). A request with a cursor runs a second count (`count_before`) to say which row number the page starts at. This experiment times each part of a directory request at M (1M employees, `acme_lab_1m`), then compares cheaper ways of producing the total. `python -m scale_lab.e10_directory_count`.

**Hypotheses (written before running):**

1. For broad requests (no filter, a status filter, a department filter) the exact count is the **largest single cost** of the request at 1M; for selective filters it is negligible.
2. A **capped count** (count at most 10,001 rows, then show "10,000+") costs a few ms whatever the match size, because it stops early.
3. The **planner's row estimate** (`EXPLAIN`, no execution) takes under 2 ms but is **inaccurate** for combined filters and for text search (more than 2× off), and is exact enough only for a single indexed filter.
4. `count_before` on a **deep page** costs about as much as the exact total, so paging deep stays proportional to the table size even with keyset pagination.

**Where a directory request's time goes** (1M employees, per SQL statement, a fresh connection; no filter and a common search):

| Statement | No filter | Search "an" |
| --- | --- | --- |
| Reporting-currency check | 0.6 ms | 0.7 ms |
| Promote due future-dated records | 1.4 ms | 1.2 ms |
| Page of 51 employees | 1.3 ms | 2.3 ms |
| Latest exchange rates | 0.5 ms | 0.7 ms |
| Totals for the page | 0.9 ms | 1.0 ms |
| **Exact `count(*)` of matches** | **28.3 ms** | **505.9 ms** |
| Whole request | 38 ms | 522 ms |

**A bigger finding: generic plans on a reused connection.** The same request repeated on **one connection** (as the app's pool does) cost 38 ms for ten requests, then jumped: request 11 took 203 ms, because the promotion statement went from **1.4 ms to 153 ms**. psycopg 3 prepares a statement after 5 uses, and Postgres then switches to a generic plan that can't use the partial index on future-dated records (the E4 effect, in the request path of every read). With `prepare_threshold=None` the promotion stayed at 1.3 ms through 24 requests. The setting is now applied in `app/db/session.py`.

**E9 re-run after that change** (directory workload, 1M, 1 worker, default pool):

| Clients | Requests/s before → after | p50 before → after | p95 before → after |
| --- | --- | --- | --- |
| 1 | 3.2 → 5.6 | 202 → 74 ms | 680 → 530 ms |
| 5 | 11.3 → 15.6 | 244 → 120 ms | 1,101 → 1,003 ms |
| 10 | 13.0 → 11.4 | 425 → 287 ms | 2,452 → 3,980 ms |
| 20 | 14.0 → 10.6 | 830 → 916 ms | 4,255 → 6,390 ms |
| 50 | 13.4 → 14.0 | 3,111 → 2,867 ms | 6,183 → 7,191 ms |

Per request at one client (p50): first page 162 → 46 ms, department + country 215 → 76 ms, by hire date 163 → 48 ms, search 657 → 512 ms. Single-client latency improved 2–3.5×; **saturation throughput did not** (11–15 req/s), because the mix is now limited by the search request's count (about 500 ms of database CPU each). Throughput at 10–20 clients is within run-to-run noise of the baseline (one run each).

**Count alternatives at 1M** (median of 5, in-process, warm):

| Request | Matches | Exact count | Capped at 10,001 | Planner estimate (value) |
| --- | --- | --- | --- | --- |
| no filter | 1,000,000 | 29.4 ms | 1.1 ms | 0.6 ms (1,000,000) |
| status = active | 862,938 | 26.5 ms | 1.7 ms | 1.0 ms (863,267) |
| department | 340,181 | 16.7 ms | 1.3 ms | 0.7 ms (342,167) |
| department + country | 102,855 | 108 ms* | 14.9 ms | 0.8 ms (104,247) |
| search "an" | 311,011 | 492 ms | 48 ms | 1.5 ms (999,935) |
| search, no match | 0 | 627 ms | 630 ms | 1.3 ms (8,279) |

\*Noisy: this median exceeded the whole request's median (79 ms) in the same run. Unfiltered, the table-statistics estimate (`pg_class.reltuples`) takes 0.33 ms and is exact here. `count_before`, the start-of-page count for a page 300,000 rows in: 114 ms (no filter), 122 ms (active), 84 ms (department), 536 ms (search). A search with no match takes **4.1 s** as a whole request, of which 3.4 s is the page query itself (the ordered scan tests every row).

**Verdicts:**

1. **Confirmed for broad requests**, and for text search: the exact count is 74% of a no-filter request (28 of 38 ms) and 97% of a search request. For selective filters it was too noisy to say.
2. **Confirmed, with a limit.** The capped count costs 1–2 ms for broad filters and 15–48 ms where the cap is reached through more rows. It does not help when there are few or no matches (630 ms for no match), because it still has to scan everything to find out.
3. **Partly refuted.** The planner's estimate costs about 1 ms and is within **1.5% for indexed filters, including the department + country combination**, which I expected to be inaccurate. It is wrong for text search (3.2× too high) and cannot say "zero" (8,279 for no match).
4. **Confirmed, and understated.** The start-of-page count for a deep page costs **3–4× the exact total** (114 ms against 29 ms), not "about as much".

**Corrections to E9:** E9 attributed the 160–200 ms single-client request to the exact count. Most of it was the generic-plan promotion statement (about 100+ ms); the count is 28 ms for an unfiltered request. The E9 conclusion that the database is the bottleneck stands, and so does the finding that more workers and a larger pool do not help.

**Not yet done:** replacing the exact count. A design that follows from the numbers: for structured filters use the planner estimate when it is above 10,000 ("about 340,000") and the exact count below that; for text search use the capped count ("10,000+"); and stop computing the start-of-page number for deep pages, or derive it more cheaply. That changes what the page shows, so it needs a decision before it is built. (Decided in E15: the exact count stays.)

---

## Summary — what the lab confirmed or changed

| Decision | Status | Evidence |
| --- | --- | --- |
| Keyset pagination for the directory (FR-1) | Confirmed | E2: `OFFSET` 2.6 s at 1M vs keyset < 1 ms |
| Denormalized `current_compensation` table | Confirmed | E3: 3–4× faster for whole-population queries; equal for pages |
| Composite index on `compensation_records` | Confirmed, essential | E3: page 1.8 ms with it, 2.9 s without, at 1M |
| Seed loading method | **Changed** to `COPY` | E1: 1.7× faster than batched `INSERT`, linear to 1M |
| Employee code generation | **Fixed** (bug) | E1: `lpad` truncation collided codes at hire #1,000,000 |
| Directory sort by compensation / live analytics at 1M | Limit recorded | 2.7–4.2 s at 1M even with the table; E5 materialized view helps 4.5–11× but not enough alone |
| Directory free-text search at 1M | Fix measured | E4: trigram GIN takes rare terms from 2–4 s to 0.4–12 ms; needs per-term plans |
| In-memory CSV import with a 10,000-row cap | Confirmed as a cap | E6: staging + SQL scales linearly with flat memory, only 1.8–2.5× faster |
| The 11 secondary indexes | Confirmed | E7: all vs essential +3.7% load time; no indexes = 4.4× slower load |
| Partition compensation records by date | Rejected for now | E8: reports 2.2–3.7× faster, lookups 1.6–3.5× slower, `id` can't stay unique |
| Pool size and worker count for the directory | No change needed | E9: workers 1 → 4 and pool 15 → 50 gave the same ~13 req/s; the database is the bottleneck. More connections caused 503s from shared-memory exhaustion (fixed in E14) |
| Prepared statements on pooled connections | **Changed** (`prepare_threshold=None`) | E10: promotion 1.4 ms → 153 ms after ~10 requests on a connection; single-client directory latency 2–3.5× lower after the fix |
