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

## E11 — Rewriting the pay-insight queries

E9 left the pay-insight views as the slowest part of the app at 1M employees (7.5–10 s each, outliers 42 s). This experiment profiles each view per SQL statement, rewrites what the profile points at, and measures again, on `acme_lab_1m` with the cache off (a fresh session per request; one run per view, so differences under about 10% are noise). The prepared-statement fix from E10 is in place for both columns.

**Method note.** Unlike E1–E10, the hypotheses below were formed *from the profile*, not before it: the first run of the profile is the evidence that suggested them.

**What the profile showed.**

1. Every view runs a "context" statement before its own, to learn which currencies were used and how many employees lack a rate. It recomputed the whole population (every employee's total pay, converted) just to count currencies: **3.5–7.3 s of every view**.
2. Outliers evaluated the population **twice** inside one statement (once for the peer medians, once for the rows): the plan shows two nested pay aggregations and 6.3 M buffers touched. A subquery used twice is computed twice; a CTE used twice is computed once.
3. Histogram used two statements (min/max, then the bins) that each recomputed the population.
4. The pay-changes report matches about 2 M change events (nearly every employee has one in a year) against every compensation record by employee, a 41 M-pair merge join on top of a window over 15.9 M records.

**What was changed** (`app/services/analytics.py`):

- `prepare()` now gets the currency information from a count of employees by currency (an index-assisted scan of `employees` with an `EXISTS` on counted pay) plus the stored rates, in Python: an employee is left out of the totals only if their currency, or the reporting currency, has no rate. Same result, no pay totals needed.
- Outliers: the shared priced population is a CTE (computed once).
- Histogram: one statement; the bounds and the bin counts share one CTE.
- Summary: one statement (E-earlier), unchanged here.

**Results** (server-side statements per request, 1M employees):

| View | Before | After | Speed-up |
| --- | --- | --- | --- |
| Summary | 13.4 s | 6.2 s | 2.2× |
| Pay by group (stats by department) | 8.8 s | 4.8 s | 1.8× |
| Same role across countries | 8.1 s | 4.5 s | 1.8× |
| Pay distribution (histogram) | 10.8 s | 4.7 s | 2.3× |
| Outliers | 39.8 s | 8.3 s | **4.8×** |
| What pay is made of (composition) | 7.9 s | 4.3 s | 1.8× |
| Pay changes (one year) | 46.0 s | 46.0 s | none |

The context statement fell from 3.5–7.3 s to 0.6–0.7 s. All 582 tests pass, including the analytics tests that check the numbers (excluded-for-no-rate counts, reporting-currency handling, single-value and empty histograms).

**Attempts that did not work:**

- *Pay changes: limiting the history window to employees with an event in the range* made the report **slower, 46 s to 128 s**, because the planner chose a worse join, and it could not have helped anyway: nearly every employee has an event in a year. Reverted.
- *Pay changes: disabling the merge join* (`enable_mergejoin = off`) gave 50 s: no better. The cost is the algorithm (a 15.9 M-row window, then 41 M pairs), not the join method.
- *Per-employee totals with a parallel hash aggregate* instead of the ordered aggregate: 2.5 s against 2.3 s. Not better.

**What is left.** Each view still spends 2.3 s (4.3 M current-pay rows aggregated into 1 M per-employee totals) plus the view's own aggregation, about 4.3–4.8 s in total. A rewrite cannot remove that pass; storing the per-employee totals does (see E3's deferred `employee_totals` and E5's materialized view, which was 4.5–11× faster). The pay-changes report needs a different algorithm (running totals from the record history) or a stored change log.

**Trade-offs of what was done:** the currency/rate logic now exists in two places (SQL for the totals, Python for the context), so they must change together; the tests exercise both. CTEs used twice are materialised, which costs memory for large populations but was a clear win here.

**Next:** pre-aggregation, a stored per-employee total kept up to date as pay changes, then re-measure these views.

---

## E12 — Pre-aggregation with a materialized view

After E11 every pay-insight view still began with the same pass: aggregating all of `current_compensation` (4.3 M rows at 1M employees) into one annual total per employee, **2.3 s** (a parallel hash aggregate was no faster). Those totals change only when pay changes. This experiment stores them in a materialized view, `employee_totals` (migration `0013`), and reads them instead.

**Hypotheses (written before measuring):**

1. Each view gets faster by about the cost of the pass it no longer does, roughly **2 s**, not the 4.5–11× that E5 measured against unrewritten queries, because E11 already removed most of the repeated work.
2. A refresh costs **a few seconds** at 1M (E5 measured 5.3 s plain and 8.4–8.9 s concurrent for a larger view) and the view is **under 100 MB**.
3. The price is **staleness**: a pay change is invisible to the pay insights until the next refresh.

**Design:**

- `employee_totals(employee_id, total)`: counted types only, in the employee's own currency, with a unique index so it can be refreshed `CONCURRENTLY` (readers never wait).
- `population()` reads it when `ANALYTICS_TOTALS_VIEW` is on; off falls back to the live aggregate. **It follows the environment: on in development, off otherwise (the production default, tuned for 10,000 employees), where the live aggregate is ample.** Tests run with it off (they check the live semantics) and one test compares the two.
- Refreshed: at bootstrap, at the end of the daily job (promotion changes pay without an API write), and by a background thread in the API after the latest write (`TOTALS_REFRESH_DELAY`; **10 s** for the figures below, default now 2 s), so a burst of writes costs one refresh; a read that promotes a due future-dated change also marks it stale. The response cache is emptied after each refresh.
- Refreshing needs ownership of the view, which the application's least-privilege role lacks, so the refresh runs through a `SECURITY DEFINER` function the role may call (it still can't alter or drop the view).

**Results** (1M employees, server time per view, cache off, one run each; live = flag off):

| View | Live (after E11) | Stored view | Speed-up |
| --- | --- | --- | --- |
| Summary | 6.9 s | 3.8 s | 1.8× |
| Pay by group (stats by department) | 5.2 s | 2.2 s | 2.3× |
| Same role across countries | 4.8 s | 2.4 s | 2.0× |
| Pay distribution (histogram) | 4.8 s | 2.7 s | 1.8× |
| Outliers | 8.6 s | 7.0 s | 1.2× |
| What pay is made of (composition) | 4.4 s | 2.1 s | 2.1× |

Cumulative against the E9/E11 starting point (13.4 / 8.8 / 8.1 / 10.8 / 39.8 / 7.9 s): summary 3.5×, stats 4.0×, role 3.3×, histogram 4.0×, **outliers 5.7×**, composition 3.8×.

- **Refresh:** 6.0–6.8 s concurrent (readers not blocked), 3.3 s plain. **Size:** 71 MB (1,000,000 rows).
- **Correctness:** the stored totals gave identical responses to the live computation for summary, stats, histogram, outliers and composition (a test, on the same data). Another test shows the snapshot behaviour: after a pay change the view still returns the old totals until refreshed, then the new ones. On the dev database a new hire was invisible for the 3 s refresh delay used in the check, then counted.

**Verdicts:**

1. **Confirmed.** The gain is 1.2–2.3×, about the 2.3 s pass, not E5's 4.5–11×. Outliers gained least (7.0 s): its time is in the peer-group medians and the join over 862k rows, not in the totals.
2. **Confirmed.** Refresh 3–7 s, size 71 MB.
3. **Confirmed, with a design to bound it.** Staleness is about the refresh delay plus the refresh itself (10 s + ~6 s at 1M; about 2 s + 0.1 s at 10,000 with the defaults). Pay changes made outside the API and not through a promotion would wait for the daily run. The directory is *not* affected: it still computes the totals of the 50 people on a page live, and the response cache is emptied on each refresh.

**Trade-offs:**

- Two sources of the same number (the live aggregate and the view) must be kept in step; the parity test guards that, but a new write path that changes pay must mark the view stale or wait for the daily refresh.
- Refresh work (6 s of database CPU at 1M) happens after writes; a steady stream of writes means a refresh about every 10 s. Pointless for this app's write rate, but it would need a different design (incremental maintenance) at much higher write volume.
- Extra moving parts: a thread, a function with elevated privileges, a migration.
- The view is a 71 MB copy that the daily job and every write burst must rebuild.

**Left:** each view still costs 2–7 s at 1M. What remains is the view's own aggregation (percentile medians over 862k employees, peer groups, conversion), the 0.6 s per-view currency count, and the pay-changes report (46 s, unchanged). A second pre-aggregation (per-group stats) or sampling would be the next step.

---

## E13 — Trigram indexes for the directory's text search

E4 measured that a `pg_trgm` GIN index turns a rare-term search at 1M employees from seconds into milliseconds, but the app was not changed because 10k employees do not need it. E10 and E11 left text search as the largest remaining directory cost: a search with no match took **4.1 s** as a whole request (3.4 s in the ordered scan that tests every row), and a common search spent 500 ms in its count. The prepared-statement fix from E10 also removes the other condition E4 found (the index was only used with per-term plans). This applies the E4 design as migration `0014` and measures it on `acme_lab_1m` with the app's own directory queries. `python -m scale_lab.e13_trigram`.

**Hypotheses (written before running):**

1. A search term of **3 or more characters that matches few employees** gets much faster: the whole directory request falls from seconds to **under 50 ms**, including the no-match case (4.1 s now).
2. **Short terms (1–2 characters)** are unchanged: a trigram index needs at least 3 characters.
3. **Very common 3-character terms** (tens of thousands of matches) improve little: the index still has to return a large number of rows, and the exact count of matches stays expensive.
4. The four indexes cost **about 160 MB** (E4) and **about 17 s** to build at 1M, and make inserts measurably slower (GIN indexes are costlier to maintain than B-trees); I expect a bulk insert of 100,000 employees to take **20–60% longer**.

**Results** (`acme_lab_1m`, whole directory request with the search term, fresh session per request, median of 5, the app's own query):

| Term | Matches | Without the indexes | With the indexes | Speed-up |
| --- | --- | --- | --- | --- |
| 2 characters ("an") | 311,011 | 540 ms | 518.4 ms | 1.0× |
| 3 characters, common ("son") | 58,728 | 615 ms | 78.5 ms | 7.8× |
| surname ("abbott") | 218 | 2,324 ms | 14.7 ms | 158.1× |
| part of a full name ("joan abb") | 3 | 2,240 ms | 20.9 ms | 107.2× |
| part of a code ("emp-0065") | 100 | 2,368 ms | 27.7 ms | 85.5× |
| part of an email ("abbott.659") | 0 | 1,704 ms | 12.7 ms | 134.2× |
| no match ("zzzqqq") | 0 | 1,645 ms | 9.1 ms | 180.8× |

- **Index cost:** four GIN indexes, **161 MB** (email 75, full name 43, last name 24, code 19 MB), built in **21 s** by the migration. A bulk insert of 100,000 employees (rolled back; best of three) took **9.3 s without and 12.83 s with** them (**+38%**).
- **Planner:** for the app's own query, `EXPLAIN` shows all four indexes used (a bitmap OR across them) for a surname, a full-name fragment, a code and an email. A test (`test_indexes.py`) now fails if a changed search expression stops matching the indexes.

**Verdicts:**

1. **Confirmed.** Every term of 3 or more characters with few matches now costs **9–28 ms** per request, 85–180× faster. The no-match search that took 1.6 s (4.1 s with the earlier term "zzzzq") takes 9 ms.
2. **Confirmed.** The 2-character search is unchanged (540 → 518 ms): trigram indexes need 3 characters.
3. **Partly refuted.** I expected little gain for a common 3-character term; "son" (58,728 matches) went from 615 to 79 ms (7.8×). It is the slowest 3+ character search because the exact count of tens of thousands of matches still costs time; that is the part the capped or estimated count (E10) would remove.
4. **Confirmed.** Size 161 MB and 21 s build as in E4; the insert slowdown (+38%) is inside the 20–60% I expected.

**Trade-offs:**

- **Writes:** about 38% slower bulk inserts, which matters for imports of tens of thousands of employees, not for single hires. (GIN indexes buffer small inserts and merge them later, so single-row inserts are cheaper than this suggests.)
- **Space:** 161 MB at 1M employees, about 40% of the size of the `employees` table (406 MB).
- **Short searches:** 1–2 character searches still scan; the page could ask for 3 characters before searching.
- **Deployment:** needs the `pg_trgm` extension (standard, available on managed Postgres including Render); building the indexes on a large live table should use `CREATE INDEX CONCURRENTLY` outside a migration transaction, which this migration does not (it holds a write lock for the build: 21 s at 1M).

**Left:** 2-character searches (about 520 ms at 1M) and the exact count for common terms.

---

## E14 — Postgres configuration (shared memory, buffers, sort memory)

E9 ran the lab database with the Docker image's defaults: `shared_buffers` 128 MB, `work_mem` 4 MB, `/dev/shm` 64 MB. It produced HTTP 503s at 50 clients (parallel queries ran out of `/dev/shm`), and the outliers plan showed a sort spilling to disk ("external merge, 6.6 MB"). This changes these settings in `docker-compose.yml`, one group at a time, and measures each on `acme_lab_1m` (8 GB container memory, 10 CPUs). The container is recreated for each change; data stays in the named volume.

**Hypotheses (written before running):**

1. **`shm_size` 64 MB → 512 MB** removes the 503 errors at 50 clients (4 workers, which gave 6–10 errors before) and does not change throughput, because the limit only decides whether a parallel query gets memory.
2. **`shared_buffers` 128 MB → 1 GB** changes little (under 15% on any view): the 4 GB working set already sits in the operating system's page cache, which Postgres reads through.
3. **`work_mem` 4 MB → 32 MB** removes the sort spills and speeds up outliers by a modest 5–15%. It is not a safe general win: E6 found `work_mem` 256 MB made one import step 8× slower, so I expect it can hurt as well as help, and I will check the other views for that.

**Results** (`acme_lab_1m`, 8 GB container, 10 CPUs; one setting changed at a time, the container recreated each time with the data kept in its volume):

**1. `shm_size` 64 MB → 512 MB.** The scenario that failed in E9 (4 workers, 50 clients, directory workload), run twice each:

| `/dev/shm` | Requests/s | p95 | HTTP 503 errors |
| --- | --- | --- | --- |
| 64 MB (default) | 13.6 / 14.2 | 10.5 / 9.2 s | **23 / 19** |
| 512 MB | 14.5 / 13.0 | 9.6 / 10.3 s | **0 / 0** |

The Postgres log had no `could not resize shared memory segment` after the change.

**2. `shared_buffers` 128 MB → 1 GB** (with the larger `/dev/shm`).

- Pay-insight views (1M employees, single client, three passes): all within about ±10% of the 128 MB baseline: summary 3.9 s, stats 2.2 s, role 2.5 s, histogram 2.7–2.8 s (3.2 s before), outliers 6.6–7.6 s (7.3 s), composition 2.0–2.3 s (2.3 s). Not distinguishable from noise.
- Directory (1 worker, two runs each):

| Clients | 128 MB: requests/s, p50 | 1 GB: requests/s, p50 |
| --- | --- | --- |
| 10 | 14.6 / 12.8, 225 / 278 ms | 16.1 / 15.2, 176 / 181 ms |
| 50 | 15.0 / 13.6, 2.5 / 3.0 s | 16.5 / 15.7, 2.5 s |

About **10–20% more throughput and 20–35% lower median latency** at 10 clients; no change in p50 at 50 clients, where requests queue.

**3. `work_mem` 4 MB → 32 MB** (with the two above). The views did not change (outliers 6.7 s against 6.6–7.6 s; no view got slower). The outliers plan's final sort stopped spilling to disk (8.9 MB in memory instead of 6.6 MB on disk), but hash steps still wrote about 100 MB of temp files. Setting `work_mem` per session to **128 MB** removed all temp files and gave 6.5 s, to **512 MB** 6.7 s, against about 7.0 s at 32 MB: a 4–8% change, inside the run-to-run spread.

**Verdicts:**

1. **Confirmed.** 42 errors in two runs became 0, throughput unchanged.
2. **Partly refuted.** The views did not change, but the directory gained 10–20%; its many small index lookups benefit from the larger buffer cache more than the big scans do.
3. **Confirmed that it is not a safe or useful win.** The spill went away and nothing sped up measurably; larger values (128 MB, 512 MB) did not hurt here either, but E6's slowdown at 256 MB shows the outcome depends on the workload, and `work_mem` applies per sort or hash node per connection, so 512 MB could use gigabytes under concurrency.

**Applied** (`docker-compose.yml`, for the local database): `shm_size: 512mb` and `-c shared_buffers=1GB`. `work_mem` stays at the default.

**Trade-offs:** `shared_buffers` takes 1 GB of the container's 8 GB (a quarter, the usual starting point) and Postgres then also relies on the operating system's cache, so the same data can sit in memory twice; `shm_size` only reserves memory when used. These are settings for the local Docker database. Render's managed Postgres has its own settings, which the free plan does not let you change, so none of this changes the hosted app.

**Left:** `max_parallel_workers_per_gather` (default 2) and `effective_cache_size` were not tested; the page cache, not Postgres's own buffers, is what holds this database.

---

## E15 — Which statements cost the most (`pg_stat_statements`)

E9 to E14 each started from one endpoint. This looks at the database as a whole: it runs a realistic mix of requests against the real server on `acme_lab_1m`, with the extension recording every normalised statement, then ranks them by total time. The point is to find anything heavy that no earlier experiment looked at. `python -m scale_lab.e15_statements`. The lab database is read-only for this run (no pay changes or imports), so write-path statements are not covered.

**Mix:** a page load (the 8 reference lists the frontend fetches), directory pages and searches, an employee profile, a filtered CSV export, the six pay-insight views and a one-month pay-changes report, with a few clients for 60 seconds.

**Hypotheses (written before running):**

1. The **pay-insight statements** (the population query, the currency count and the pay-changes report) account for **over 80%** of total database time, though they are a small share of the request count.
2. **No statement outside the analytics and directory code** takes more than 5% of total time.
3. The **reference lists** (departments, titles, levels, countries, currencies, companies, catalog) are called far more often than anything else but cost **under 1%** in total.

**Results** (`acme_lab_1m`, one server, 4 clients for 90 s: 174 requests, 0 errors; the database spent **417 s** executing statements, spread across parallel workers). Top statements by total time:

| Statement | Share of database time | Total | Calls | Mean per call |
| --- | --- | --- | --- | --- |
| Pay-changes report (one-month range) | 49.4% | 206.1 s | 8 | 25,768 ms |
| Currency count (run first by every pay-insight view) | 11.4% | 47.7 s | 48 | 994 ms |
| Outliers (main statement) | 11.2% | 46.6 s | 8 | 5,823 ms |
| Summary (main statement) | 6.6% | 27.7 s | 8 | 3,462 ms |
| Composition | 6.0% | 25.1 s | 8 | 3,139 ms |
| Same role across countries | 4.9% | 20.5 s | 8 | 2,559 ms |
| Histogram | 4.8% | 20.1 s | 8 | 2,510 ms |
| Stats by department | 4.5% | 18.9 s | 8 | 2,360 ms |

Everything else together is **under 1.5%**. The next statements down: the directory's count with a department filter (0.7%, 385 ms mean), the unfiltered count (0.2%, 101 ms), the directory page (0.1%, 38 ms), a profile's totals (under 0.1%), and the nine reference lists (too small to appear in the top 15).

**Verdicts:**

1. **Confirmed, and more so than expected.** The pay-insight statements are **98.8%** of database time (I predicted over 80%).
2. **Confirmed.** Nothing outside the analytics code exceeds 1%; the largest directory statement is 0.7%.
3. **Confirmed.** The reference lists cost nothing worth measuring.

**New findings:**

- **The pay-changes report is half of all database time**, at 25.8 s per call even for a **one-month** range. E11 saw 46 s for a year. The one-month result means its cost is the window over all history, not the number of events, and that it has not been tried with a narrow range: E11's "only employees with an event" filter was tested on a one-year range only (where nearly everyone has an event).
- **The currency count is the second most expensive statement (11.4%)** although it only reports which currencies are in use. Every view runs it first, and it costs about 1 s per call under load (0.6 s alone).
- **Decision on the directory count (E10): leave it exact.** In this mix it is 0.2–0.7% of database time. An approximate count would change what the page shows to save almost nothing, except for common searches (about 500 ms), which E13's indexes only help for 3+ characters.

**Limits of this run:** read-only (no pay changes, imports or CSV imports), one server process, a fixed request mix that weights the pay insights equally with page loads. A real user mix would put less weight on the pay insights, but they are still the heaviest requests by two orders of magnitude.

**Next:** (1) the pay-changes report for narrow date ranges; (2) the currency count, which can use the stored totals instead of scanning pay.

---

## E16 — The pay-changes report on narrow date ranges (negative result)

E15 found the pay-changes report is half of all database time, at 25.8 s per call even for a one-month range. E11 had tried limiting its history window to employees with an event, but only on the one-year range (where nearly everyone has an event) and it was slower. This asks whether that idea works when few employees have changed. Code was restored afterwards; this section records what was learned.

**Baseline** (`acme_lab_1m`, in-process, fresh session, one run each):

| Range | Time | Employees with a change | Events |
| --- | --- | --- | --- |
| 1 week | 1.6 s | 0 | 0 |
| 1 month | 26.9 s | 109,769 | 109,769 |
| 1 quarter | 28.2 s | 146,292 | 148,282 |
| 1 year | 45.4 s | 862,938 | 1,988,176 |

The cost has a floor of about 25 s that barely depends on the range, because the report windows over every compensation record (15.9 M rows) to find when each record stopped applying.

**Hypothesis (formed from the E15 finding, before the variants were timed; the factors are my estimate from the share of employees involved):** restricting that window to the employees who have an event in the range makes a month about **5× faster** (11% of employees), a quarter about 4×, and does not help a year (everyone is involved).

**Tried:** the same report with `events` as a CTE and the records restricted (a) with `IN (select employee_id from events)` and (b) with a join to the distinct employees of the events.

| Range | Original | `IN` on a CTE | Join on a CTE |
| --- | --- | --- | --- |
| 1 month | 26.9 s | 19.1 s | 19.9 s |
| 1 quarter | 28.2 s | 26.1 s | 23.9 s |
| 1 year | 45.4 s | **125.1 s** | **96.1 s** |

**Verdict: refuted.** The gain for a month is **1.4×**, not 5×, and the year gets **2–3× slower**. The plan explains why: the restricted history is 3.6 M rows instead of 15.9 M, but fetching it is a nested loop of one index lookup per employee (110 k lookups into a 2.9 GB table, 10.9 s), then a sort of 3.6 M rows (2.4 s) and a window (1.2 s). Forcing hash joins (`enable_nestloop = off, enable_mergejoin = off`) reached 14 s for the month, still far from the 5× hoped for. On the wide range the planner's choices are worse still.

**Other things that did not work:** disabling the merge join on the year (50 s, from E11).

**What would work** (not built; the options and their costs):

1. **A stored change log, as a materialized view or a table of events** (employee, day, before total, after total, currency, reasons), built once over all history and read by date range. The report would take milliseconds. The price: building it windows over all history (probably 30–60 s at 1M employees), so it could only be refreshed rarely (daily), and events from today would be missing until then; or it must be maintained on every pay write, which means reproducing the report's rules (corrections, relocations, same-day records) in write code.
2. **Running totals from deltas** instead of a range join: still reads all history, but could avoid the 41 M-pair join. A rewrite of the report's logic with real risk of changing results.
3. **Accept it:** the report is infrequent, takes about 25 s at 1M employees and about 0.4 s at the 10,000 the brief asks for, and the response cache makes a repeated request free.

**Recommendation:** accept for now (option 3), and revisit with option 1 if the report matters at scale.

---

## E17 — VACUUM and the visibility map

Postgres can answer a count from an index alone (an index-only scan) only for pages it knows have no row versions hidden from some transaction: the *visibility map* records those pages, and `VACUUM` sets it. For any other page the scan has to visit the table ("Heap Fetches"). E10's plan for the directory's count showed `Heap Fetches: 20,005` even on a freshly built database. This measures what that costs, what ordinary writes do to it, and what restores it. `python -m scale_lab.e17_vacuum` on `acme_lab_1m`.

**As found (before any change):** autovacuum has already run on every table, so 96.0% (history), 97.5% (current pay) and 96.6% (employees) of pages are marked all-visible. My expectation that a bulk load would leave the tables unvacuumed was wrong: since Postgres 13 autovacuum also triggers on inserts.

**Hypotheses (written before running):**

1. A manual `VACUUM` brings the remaining pages to about 100% all-visible and cuts `Heap Fetches` on the count queries to near zero, but speeds them up only by **10–30%**, since just 3–4% of pages need a visit.
2. **Writes degrade it quickly and by more:** updating 5% of employees (50,000 rows) spreads that over most of the table's pages, so the same count slows by **1.5× or more**.
3. **Autovacuum will not repair that on its own:** its trigger for updates is 20% of the table (plus 50 rows), so a 5% burst leaves the table degraded indefinitely, until a manual `VACUUM` or a larger change.

**Results** (`acme_lab_1m`; median of 5; "fetches" are the index-only scan's heap fetches from `EXPLAIN ANALYZE`; `VACUUM (ANALYZE)` of `employees` and `current_compensation` took 4.4 s):

| Moment | Count all employees | Count active employees | Currency usage (E15's 11%) |
| --- | --- | --- | --- |
| As found (autovacuum had run) | 23.2 ms, 5,013 fetches | 24.5 ms, 4,426 | 735 ms |
| After a manual `VACUUM` | 32.6 ms, **0** | 26.3 ms, 0 | 524 ms |
| After updating 5% of employees (50,000 rows) | **196 ms, 1,049,258 fetches** | **75.6 ms, 905,201** | 656 ms |
| 180 s later, nothing done | **164 ms, 1,049,220** | **69.3 ms, 905,201** | 520 ms |
| After `VACUUM` again | 32.5 ms, 0 | 25.1 ms, 0 | 577 ms |

The update left 50,000 dead tuples; 180 s later 49,258 were still there. (The "pages all visible" percentage in `pg_class` is only refreshed by `VACUUM`/`ANALYZE`, so it kept saying 100% after the updates; the fetch counts are the true signal.)

**Verdicts:**

1. **Refuted.** The manual `VACUUM` removed the 5,000 heap fetches but did not make the counts faster (23 → 33 ms and 25 → 26 ms are noise): 5,000 fetches on a million-row count is 0.5% of the rows. As found, the database was already in good shape.
2. **Confirmed, and worse than predicted.** Updating 5% of employees made the count **6× slower** (33 → 196 ms) and the active count **3× slower**. Fifty thousand updates touched nearly every one of the table's 33,800 pages (about 1.5 rows per page), so about every row needed a heap fetch: 1.05 M fetches, almost the whole table.
3. **Confirmed.** Three minutes later autovacuum had done nothing and the counts were just as slow. Its trigger for updates is 20% of the table plus 50 rows (about 200,000 here), so a 5% burst is invisible to it. Only a manual `VACUUM` (4.4 s) restored it.

**Currency usage did not change** (520–735 ms every time) because its plan does not use an index-only scan on `employees`, so the visibility map cannot help it. Its cost is elsewhere: scanning 862k active employees and probing `current_compensation` for each.

**What this means:**

- A table that is updated in small, scattered amounts loses its index-only scans without anything being wrong or logged. At 1M employees it costs 100–160 ms per directory request (the exact total counts). At 10,000 employees the same effect is a fraction of a millisecond, so nothing is needed by default.
- Remedies when it matters: run `VACUUM` after a bulk update or import, or lower `autovacuum_vacuum_scale_factor` on `employees` (for example 0.02 triggers at about 20,000 changes here) so it repairs itself within a minute. Both are operational settings; no application change.
- Manual `VACUUM` is cheap (4.4 s for two tables with 5.3 M rows) and does not block reads or writes.

**Not done:** `compensation_records` (the 2.9 GB history) was not vacuumed or updated: it is append-only, so it cannot lose its visibility map this way. A real workload that updates employees, such as relocations and status changes, is the case that does.

---

## E18 — The pay-insight context query (currency count)

Every pay-insight view first runs `currency_usage` (added in E11): a count of active employees by currency, restricted to employees who have some pay that counts toward the total, so it can say which currencies are in use and how many employees lack an exchange rate. E15 found it is the second-largest statement (11.4% of database time, about 1 s per call under load), and E17 showed the visibility map cannot help it. This looks at its plan and compares ways of answering the same question on `acme_lab_1m`. `python -m scale_lab.e18_currency_usage`.

**Hypotheses (written before looking at the plan):**

1. The time is spent **testing every active employee (862 k) for counted pay**: a probe into `current_compensation` joined to `compensation_types` for each employee, not in the grouping.
2. Answering from the **stored totals** (`employee_totals`, one row per employee with counted pay) removes that probe: a join to the stored totals should be **3–5× faster**, under 200 ms.
3. Replacing the join to `compensation_types` with the **literal list of counted type ids** helps only modestly (under 30%).
4. All variants return **identical counts**.

**Where the time goes** (`EXPLAIN (ANALYZE, BUFFERS)` of the as-built query, cold cache, 1.39 s): Postgres does **not** probe pay once per employee. It scans all 4.3 M rows of `current_compensation` (parallel), keeps the 3 M that count toward the total, builds a hash of them (32 batches, spilled to temp files), and semi-joins the 862 k active employees against it. The cost is reading and hashing the pay rows.

**Results** (median of 5 after a warm-up, warm cache; unfiltered, and for the Engineering department in India):

| Variant | Unfiltered | Speed-up | Department + country | Speed-up |
| --- | --- | --- | --- | --- |
| V0, as built (live pay, joined to the types) | 824 ms | 1.0× | 223 ms | 1.0× |
| V1, with the literal list of counted type ids | 754 ms | 1.1× | 129 ms | 1.7× |
| V2, join to the stored totals | 288 ms | 2.9× | 122 ms | 1.8× |
| V3, `EXISTS` on the stored totals | 268 ms | 3.1× | 115 ms | 1.9× |

All four returned **identical counts** (12 currencies and 862,938 employees unfiltered; 1 currency and 88,674 employees filtered).

**Verdicts:**

1. **Right in essence, wrong in mechanism.** The cost is reading the pay rows, but as one hash semi-join over all of them, not a probe per employee.
2. **Mostly confirmed.** Using the stored totals is **3.1× faster unfiltered (824 → 269 ms)** and **1.9× with filters (223 → 115 ms)**. I predicted 3–5× and under 200 ms: the filtered case meets the latter, the unfiltered one does not (what remains is a parallel scan of 862 k employees, about 200 ms).
3. **Partly refuted.** The literal type-id list gained 8% unfiltered (as predicted) but **42% with filters** (223 → 129 ms), because when few employees qualify, skipping the join to `compensation_types` matters more.
4. **Confirmed.**

**Applied:** when the stored totals are in use (development), `currency_usage` asks `employee_totals` who has counted pay (V3). That also makes it exact for the people the view reads: both now come from the same snapshot, where before a hire made after the last refresh counted in the currencies but not in the totals. Otherwise (production, 10,000 employees) it keeps the live query, V0: it takes a few milliseconds there and is always current.

**Effect on the views** (1M employees, single client, with the stored totals): each pay-insight view got **0.2–0.5 s faster**: summary 3.9 → 3.7 s, stats 2.2 → 2.0 s, role across countries 2.5–2.9 → 2.2 s, histogram 2.7–3.0 → 2.4 s, composition 2.0–2.3 → 1.9–2.1 s, outliers 6.6–7.0 → 6.3–6.5 s. No change at 10,000 employees.

**Trade-off:** the development path now depends on the stored totals for its context as well as its numbers, so a stale snapshot is stale in both consistently. The parity test (stored against live, including the excluded-no-rate count and the rate dates) covers it.

**Left:** the unfiltered cost is now mostly the parallel scan of employees; a covering index on `(status, currency)` could make that index-only, an index experiment for later.

---

## E19 — Index variants (covering, partial, BRIN)

E7 settled which ordinary indexes the app needs. This tests three other kinds against the queries that E15 and E18 found expensive, on `acme_lab_1m`, as an experiment only: each index is created, measured and dropped, and the application and its migrations are not changed. `python -m scale_lab.e19_indexes`.

**Candidates:**

- **I1, a partial covering index for the currency count:** `(currency, id) WHERE status = 'active'`. The count reads only these two columns of the 862 k active employees, so it could be answered from a small index instead of scanning the 406 MB `employees` table.
- **I2, a wide partial covering index for the pay-insight population:** `(id) INCLUDE (company_id, department_id, job_title_id, job_level_id, current_country, currency) WHERE status = 'active'`. Every view starts by reading those columns of the active employees.
- **I3, BRIN on `compensation_records(effective_date)`:** a tiny index for date ranges (the pay-changes report), which works only if the table's physical order follows the date.

**Hypotheses (written before running):**

1. **I1:** the unfiltered currency count falls from about 270 ms to **under 150 ms** (at least 1.7×); the filtered one (department + country) does not change, since its work is elsewhere.
2. **I2:** the pay-insight views improve by **10% or less**: their main statements are dominated by the joins, the aggregation and the median, not by the scan of employees. Inserting 100,000 employees gets **15–25% slower** with it.
3. **I3:** BRIN is **useless here**. Records are inserted employee by employee, so their dates are scattered across the table (correlation near zero) and the planner will not use it, or will gain nothing for a one-month range.
4. **Sizes:** I1 about 25 MB, I2 about 70 MB, I3 under 1 MB.

**Results** (`acme_lab_1m`, in-process timings, best of 3 for the views, median of 5 for the queries; the 100,000-employee insert is the best of 3; one run each):

| | Baseline | I1 partial covering | I2 wide partial covering | I3 BRIN |
| --- | --- | --- | --- | --- |
| Index size | n/a | 26.0 MB | 55.5 MB | 0.1 MB |
| Build time | n/a | 1.0 s | 1.0 s | 6.0 s |
| Used by the planner for the currency count | n/a | **no** | **no** | no |
| Currency count, unfiltered | 341.6 ms | 263.2 ms | 274.7 ms | 342.5 ms |
| Currency count, filtered | 106.5 ms | 109.0 ms | 116.7 ms | 112.4 ms |
| One-month date-range count on history | 677.1 ms | 729.9 ms | 935.0 ms | **2019.9 ms (index used)** |
| Insert of 100,000 employees | 12.03 s | 12.48 s | 12.87 s | 12.25 s |
| Views, time relative to baseline (summary, stats by department, role across countries, histogram, composition, outliers) | 1.00 | 1.03 / 1.08 / 1.08 / 1.24 / 1.11 / 1.17 | 0.96 / 1.10 / 1.06 / 1.26 / 1.07 / 1.08 | 1.01 / 1.02 / 1.08 / 1.05 / 1.13 / 1.06 |

Baseline views: summary 3.6 s, stats by department 1.8 s, role across countries 2.1 s, histogram 2.3 s, composition 1.8 s, outliers 6.4 s.

**An upper bound for I1.** The planner did not choose I1 (nor an index-only scan of it). With sequential scans disabled it chose a bitmap scan on the existing status index, and the count took 288–435 ms against 227–525 ms with the planner's own choice: no gain, so I1 would not help this query even if it were picked.

**Verdicts:**

1. **Refuted.** I1 did not get the unfiltered count under 150 ms: the planner never used it, and the 263 ms against the 342 ms baseline is the machine's run-to-run spread (E18 measured 269 ms with no index at all).
2. **Confirmed that it does not help, for a different reason than predicted.** I2 changed the views by between 0.96× and 1.26× (all noise: the planner did not use it either). Its insert cost was +7% (12.0 → 12.9 s), within the ±10% spread of this test, below the 15–25% I expected.
3. **Refuted, and worse than predicted.** BRIN was used for the one-month date range and made it **3× slower** (677 → 2,020 ms): the table's physical order does not follow the date (correlation 0.021), so every block range matches and Postgres rechecks almost the whole table through a bitmap scan, instead of the parallel sequential scan it used without the index.
4. **Mostly confirmed.** Sizes 26 MB (I1), 55.5 MB (I2, smaller than the 70 MB guessed), 0.1 MB (I3).

**Conclusions:**

- None of the three is adopted. The planner already scans `employees` in parallel quickly (about 200 ms of the unfiltered count); an index cannot cut a cost that is mostly the hash join with the stored totals.
- A BRIN index needs the data's physical order to follow the indexed column. This table is loaded employee by employee, so dates are scattered; it would work on an append-only log written in time order, which this is not.
- Noise: the same view varied by up to 25% between states (histogram 2.3 to 2.9 s), so these tests could not show an effect under about 25% on a single view.

**Not tested:** a plain B-tree on `effective_date` (E8 found it did not help a date-range query on this table), and covering indexes for the directory's page query, which already takes 1–2 ms.

---

## E20 — Parallel query workers and `effective_cache_size`

E14 left two settings untested. **`max_parallel_workers_per_gather`** (default 2) is how many extra processes one query may use for big scans and aggregations; the plans in E10 and E18 show queries already using 2. **`effective_cache_size`** (default 4 GB) tells the planner how much data it can expect to find cached; it changes which plan is chosen, and allocates nothing. Both are set per database (`ALTER DATABASE ... SET`) on `acme_lab_1m` and reset afterwards, so they take effect for new connections without restarting the server. `python -m scale_lab.e20_planner_settings`.

**What is measured, for each value:** one client (the six pay-insight views, best of 3; the directory's first page, a filtered page and a search, median of 5), and ten clients on the directory workload for 12 s (the real server, as in E9).

**Hypotheses (written before running):**

1. **One client:** more workers make the big queries faster, with diminishing returns: 0 workers is **1.5–2.5× slower** than 2 on the views, and 4 or 8 workers are **at most 1.3× faster** than 2.
2. **Ten clients:** more workers per query *lower* throughput on the directory workload, because queries compete for the same 10 CPUs: 0 workers gives the **highest** throughput and 8 the lowest, by 10–30%.
3. **`effective_cache_size`:** values from 1 GB to 6 GB change **no query by more than 10%** and no plan, because the plans here are not near the index-versus-scan threshold.

**A caveat before the numbers.** The six views ran about **twice as fast in this experiment as in E18 and E19** (summary 1.6 s here against 3.7 s there; the original profiling script run straight afterwards gave 1.8–2.0 s, so it is the machine or database state, not this script). I do not know why: nothing in the code changed in between. Compare only the rows *within* each table below, never across experiments.

**`max_parallel_workers_per_gather`** (`acme_lab_1m`; one client; views best of 3, time and ratio to the default of 2 workers):

| | summary | stats by department | role across countries | histogram | composition | outliers |
| --- | --- | --- | --- | --- | --- | --- |
| 0 workers | 1.75 s (1.09×) | 1.44 s (1.50×) | 1.75 s (1.58×) | 1.33 s (1.10×) | 2.49 s (2.55×) | 3.29 s (1.05×) |
| 2 workers | 1.61 s (1.00×) | 0.96 s (1.00×) | 1.11 s (1.00×) | 1.21 s (1.00×) | 0.97 s (1.00×) | 3.15 s (1.00×) |
| 4 workers | 1.62 s (1.00×) | 0.97 s (1.01×) | 1.12 s (1.02×) | 1.19 s (0.99×) | 0.72 s (0.73×) | 3.26 s (1.04×) |
| 8 workers | 1.65 s (1.02×) | 0.97 s (1.01×) | 1.10 s (1.00×) | 1.22 s (1.01×) | 0.73 s (0.75×) | 3.15 s (1.00×) |

Ten clients on the directory workload for 12 s (and, last column, the one-client directory first page / department + country page / search):

| Setting | Requests/s | p50 | p95 | Errors | One-client pages |
| --- | --- | --- | --- | --- | --- |
| 0 workers | 25.8 | 81 ms | 1286 ms | 0 | 31.7 / 17.4 / 10.0 ms |
| 2 workers | 24.0 | 143 ms | 1792 ms | 0 | 21.4 / 18.9 / 10.9 ms |
| 4 workers | 23.5 | 150 ms | 1695 ms | 0 | 23.3 / 20.6 / 9.5 ms |
| 8 workers | 22.7 | 153 ms | 1857 ms | 0 | 21.5 / 20.4 / 10.1 ms |

**`effective_cache_size`** (views, ratio to the default of 4 GB):

| | summary | stats by department | role across countries | histogram | composition | outliers |
| --- | --- | --- | --- | --- | --- | --- |
| 1GB | 1.63 s (0.99×) | 0.96 s (0.99×) | 1.12 s (0.92×) | 1.21 s (0.93×) | 0.95 s (0.86×) | 3.19 s (1.00×) |
| 4GB | 1.66 s (1.00×) | 0.98 s (1.00×) | 1.21 s (1.00×) | 1.30 s (1.00×) | 1.10 s (1.00×) | 3.21 s (1.00×) |
| 6GB | 1.72 s (1.04×) | 0.98 s (1.00×) | 1.16 s (0.96×) | 1.20 s (0.93×) | 0.98 s (0.89×) | 3.24 s (1.01×) |

| Setting | Requests/s (10 clients) | p50 | p95 | Errors | One-client pages |
| --- | --- | --- | --- | --- | --- |
| 1GB | 23.9 | 138 ms | 1655 ms | 0 | 21.1 / 18.4 / 8.9 ms |
| 4GB | 24.2 | 144 ms | 1727 ms | 0 | 25.0 / 22.6 / 10.6 ms |
| 6GB | 24.1 | 150 ms | 1657 ms | 0 | 22.4 / 23.0 / 7.7 ms |

**Verdicts:**

1. **Mostly confirmed.** With no parallel workers three views were **1.5–2.6× slower** (stats 1.50×, role across countries 1.58×, composition 2.55×) and three barely changed (summary 1.09×, histogram 1.10×, outliers 1.05×). Beyond the default of 2, more workers gained nothing except on composition (**0.73–0.75×, about 1.35× faster**); the other five views were within 4% of the default.
2. **Confirmed, in direction and size.** Under ten clients, throughput fell as workers rose: 25.8 → 24.0 → 23.5 → 22.7 requests/s for 0, 2, 4, 8 workers (0 is **8% above the default and 14% above 8**), and the median latency of a directory request was 81 ms with none, 143 ms with 2 and 153 ms with 8. Single-client directory pages (10–30 ms) did not change.
3. **Confirmed.** `effective_cache_size` from 1 GB to 6 GB changed no view or page by more than the run-to-run spread (the largest, composition at 0.86–0.89×, is the same query that varied most in the workers table), and throughput was 23.9, 24.2 and 24.1 requests/s.

**Trade-off and decision.** Parallel workers help a lone heavy query (up to 2.6×) and cost a busy server a little (up to 14% throughput and a doubled median latency for small queries, since each query takes CPUs from the others). The default of 2 is a sensible middle: it keeps nearly all of the one-client gain, and 4 or 8 add nothing here. **No setting is changed.** `effective_cache_size` matters when the planner chooses between an index and a scan near the break-even point, which none of these queries is.

**Left:** nothing on this list. (The settings were applied with `ALTER DATABASE` and reset afterwards; the lab database has no per-database settings.)

---

## E21 — Compression and cache headers

The first look at the application layer (the 10,000-employee profile in the conversation behind E21) found the server sends everything uncompressed: the hosted bundle is 345 KB of JavaScript, the CSV export 1.5 MB, a directory page 20 KB, and a pay-insight view up to 104 KB. On Render the browser is on the other side of a real network, so bytes become time. This measures what gzip saves, what it costs in CPU, and what cache headers add for repeat visits. `python -m scale_lab.e21_compression`. Local servers on the 10k database; transfer time is simulated by limiting the download rate (`curl --limit-rate`), because there is no network latency on one machine.

**Hypotheses (written before building anything):**

1. gzip makes JSON responses **5–10× smaller** (a directory page from 20 KB to about 3 KB), the JavaScript bundle about **3×** smaller (345 KB to about 110 KB), and the CSV export about **5×** smaller (1.5 MB to about 300 KB).
2. The CPU cost is small at a moderate level: **under 3 ms** for a directory page and **under 60 ms** for the 1.5 MB export at level 5, while Starlette's default level 9 costs several times more for almost no extra saving.
3. On a **4 Mbps** link the first load of the bundle falls from about 700 ms to about **230 ms**, and the export download from about 3 s to about **0.6 s**; on a fast link (100 Mbps) the difference is negligible.
4. **Cache headers:** marking the hashed bundle files `immutable` removes the bundle from every repeat visit (0 bytes). ETag revalidation of the nine reference lists saves little *after* gzip (they total only a few KB), so I expect it is not worth building.

**What changed** (`app/main.py`, `app/web.py`): responses of 1 KB or more are gzipped at level 5 when the browser accepts it (`COMPRESS_RESPONSES`, on by default); the build's hashed files under `/assets` are served `Cache-Control: public, max-age=31536000, immutable` (they are named after their content, so they never change under their name); `index.html` stays `no-cache`. A bug found on the way: my timing middleware handed on a *stream*, which the gzip middleware compresses regardless of size, so a 15-byte `/health` response was being gzipped into a bigger one; the middleware now passes on whole bodies and a test covers it.

**Results** (the site on the 10,000-employee database; two servers, compression off and on; 10 requests each, median):

| Response | Plain | Gzip | Ratio | Extra time on this machine | Transfer at 4 Mbps | Transfer at 100 Mbps |
| --- | --- | --- | --- | --- | --- | --- |
| JavaScript bundle | 341.3 KB | 101.4 KB | 3.4× | +10.5 ms | 699 → 208 ms | 28 → 8 ms |
| stylesheet | 11.5 KB | 3.2 KB | 3.6× | +0.4 ms | 24 → 7 ms | 1 → 0 ms |
| reference: countries | 14.2 KB | 3.0 KB | 4.7× | +0.5 ms | 29 → 6 ms | 1 → 0 ms |
| directory page (50) | 20.0 KB | 3.3 KB | 6.0× | +1.1 ms | 41 → 7 ms | 2 → 0 ms |
| pay insight: outliers | 101.7 KB | 17.6 KB | 5.8× | -0.3 ms | 208 → 36 ms | 8 → 1 ms |
| CSV export (all 10k) | 1,463.5 KB | 387.8 KB | 3.8× | +46.4 ms | 2,997 → 794 ms | 120 → 32 ms |

Transfer times are computed from size and link speed (size ÷ 500 KB/s and ÷ 12.5 MB/s), not measured: `curl --limit-rate` lets short transfers burst, so for the small files it reported times faster than the link allows. For the one large transfer it was reliable and agrees: the **CSV export took 2,792 ms plain and 600 ms gzipped at 4 Mbps** (4.7× faster). The extra time column is the measured cost on this machine with no network delay.

**gzip levels on the real payloads** (size, CPU time per compression):

| Payload | Level 1 | Level 5 (chosen) | Level 6 | Level 9 (Starlette's default) |
| --- | --- | --- | --- | --- |
| JavaScript bundle | 119 KB, 3.85 ms | 101 KB, 9.25 ms | 100 KB, 12.08 ms | 100 KB, 15.23 ms |
| directory page (50) | 4 KB, 0.08 ms | 3 KB, 0.13 ms | 3 KB, 0.16 ms | 3 KB, 0.34 ms |
| CSV export (all 10k) | 462 KB, 13.93 ms | 388 KB, 35.3 ms | 381 KB, 45.55 ms | 375 KB, 74.0 ms |

**Cache headers:** the hashed bundle is served with `public, max-age=31536000, immutable`; a request with its `ETag` returns **304 with 0 bytes** (static files already had validators). With `immutable` the browser does not even ask on a repeat visit.

**Verdicts:**

1. **Mostly confirmed.** JSON shrank 4.7–6.0× (predicted 5–10×), the bundle 3.4× (about 3×), but the CSV export only **3.8×** (predicted 5×): numbers compress less than text.
2. **Confirmed.** Level 5 costs 0.13 ms on a directory page and 35 ms on the export (the full request was 46 ms slower); level 9 costs 2 to 2.1× more CPU for 1.4% (bundle) to 3% (export) smaller output, and level 1 gives 18 to 19% larger files. The bundle costs about 10 ms of CPU *per request* because static files are compressed again every time.
3. **Confirmed for slow links, and a loss on fast ones.** At 4 Mbps the bundle's transfer falls from about 700 ms to about 210 ms and the export's from 3.0 s to 0.8 s (measured 2.8 s to 0.6 s). At 100 Mbps the transfer saving (about 8 ms for the bundle, 90 ms for the export) is smaller than or close to the compression time, and the **export was 40 ms slower gzipped (463 → 503 ms)**.
4. **Confirmed.** Reference lists after gzip total under 6 KB, so ETag revalidation for them was not built. Server-Timing, being inside the compression, does not include the compression time.

**Decision:** keep gzip on by default (the hosted app is reached over the internet, where it wins by a wide margin) at level 5; keep the immutable cache headers. **Trade-offs:** CPU on every large response (about 10 ms per bundle request on Render's small CPU, which a precompressed file or a cache of the compressed bundle would remove); worse for very fast links.

**Left:** precompress the bundle at build time instead of per request.

---

## E22 — The CSV export

E21's profile of the application layer found the export is the slowest ordinary request at 10,000 employees: 455 ms in total, of which the database is 167 ms and **287 ms is Python**. This looks at where that Python time goes and removes it without changing a byte of the file. `python -m scale_lab.e22_export`.

**What the code does today:** `export_employees` loads every matching employee as a full ORM `Employee` object with its department, title and level (three joined relationships), then fetches the totals in batches of 1,000 employees (11 queries at 10k), converts each total to the reporting currency in Python, and `directory_csv` writes the rows with `csv.writer`. On the 10k database that is about 370 ms to load and convert and 73 ms to write.

**Hypotheses (written before changing anything):**

1. Most of the loading time is **building ORM objects** (about 40,000 instance loads for 10,006 employees) and the **11 batched totals queries**, not the SQL itself. Selecting just the columns the file needs, with the names joined in SQL, and computing the totals in the same statement, cuts "load and convert" from about 370 ms to **under 150 ms**.
2. The CSV writer has a little slack: trimming per-row work takes the 73 ms to **about 45 ms or less**.
3. The whole export falls from about 445 ms to **under 200 ms** in-process, and the file is **byte-for-byte identical** (checked against the old output for unfiltered, filtered, sorted-by-pay and other-currency exports).
4. The saving scales with rows: a 40,000-row export at 1M employees (one country) falls by a similar factor.

**What changed** (`app/services/directory.py`, `app/services/exports.py`):

- `export_employees` now selects just the exported columns (`ExportPerson`, a plain tuple type) with the department, title and level names joined in SQL, in the directory's order, instead of loading an `Employee` object with three related objects for every row.
- The totals come from **one** query restricted to exactly the exported employees (the ids passed as a single array parameter), instead of one query per 1,000 employees (11 at 10k).
- `directory_csv` builds each row from a generator and works out the older-rate date once per currency instead of once per row.
- The exported bytes are unchanged.

**Results** (in-process, a fresh session per run, median of 3; the old column is the code before the change):

| Export | Rows | Old | New | Speed-up |
| --- | --- | --- | --- | --- |
| Everyone (10,000-employee database) | 10,006 | 440 ms | 170–190 ms | 2.3–2.6× |
| One country (India) | 3,114 | 132 ms | 59–62 ms | 2.1–2.2× |
| By pay, descending, two countries | 2,624 | 126 ms | 71–79 ms | 1.6–1.8× |
| By level, in euros, one country | 599 | 34 ms | 18–22 ms | 1.5–1.9× |
| Search "an", newest first | 3,071 | 141 ms | 71–73 ms | 1.9–2.0× |
| Active, in rupees, two countries | 3,034 | 150 ms | 56–59 ms | 2.5–2.7× |
| **One country at 1M employees (Singapore)** | **40,292** | **1,892 ms** | **994–1,128 ms** | **1.7–1.9×** |

The profile at 10k went from 370 ms to load and convert plus 73 ms to write the file, to **128–134 ms plus 39 ms**. As a real HTTP request (the export button) the 10k export fell from 455 ms to **178 ms**: database 167 → 80 ms (5 queries instead of 15) and Python 287 → 94 ms.

**The files are byte-for-byte identical** to the old code's: SHA-256 hashes and a byte comparison match in all six 10k cases and in the 1M export (the 10k "everyone" file hashes to `325228a81541` before and after).

**A first attempt that was slower at scale.** Version 1 joined the totals into the main statement as a grouped subquery (one query, and 10k went to 171 ms, even faster). At 1M the same Singapore export took **3,430 ms, against 1,892 ms before**: a grouped join makes Postgres total *every* employee's pay (the 2.3 s pass over 4.3 M rows from E12) even to export 40,000 people. The old batched `IN` lists had restricted that work to the selected employees without anyone noticing. Fetching the totals in a second query for exactly the exported ids fixes it.

**Verdicts:**

1. **Confirmed.** Load and convert fell from 370 ms to 128–134 ms (target: under 150). The saving is the ORM objects and the batched queries; the SQL itself got cheaper too (database time 167 → 80 ms).
2. **Confirmed.** The writer fell from 73 ms to 39 ms (target: 45 or less).
3. **Confirmed.** The whole export is 170–190 ms in-process (target: under 200), identical bytes.
4. **Partly confirmed.** At 1M the saving is **1.7–1.9×**, less than at 10k (2.3–2.6×): the totals pass for 40,000 employees and the database now dominate, and the Python saving is a smaller share.

**Trade-offs:**

- The export no longer goes through the `Employee` model: a new exported column needs adding in two places (the select and `ExportPerson`) instead of one.
- A fixed hazard avoided: passing ids as one array parameter keeps working for very large exports, where one bound parameter per id would hit Postgres's 65,535-parameter limit.
- The whole file is still built in memory (about 1.5 MB for 10,000 employees, around 150 MB for a million); streaming would not make it faster, only lighter.

**Left:** the export of everyone at 1M employees (a 150 MB file) was not measured; it is not a realistic request.

---

## E23 — Serialisation (negative result)

The application-layer ranking after E21 put serialisation third: at 10,000 employees a directory request spends about 6 ms in Python, and I expected `orjson` with lighter response handling to **roughly halve it**. This measures how much of a response is serialisation before adding a dependency. `python -m scale_lab.e23_serialisation`.

**Prediction (from the ranking, before measuring):** serialisation is a large part of the directory's 6 ms of Python, and `orjson` takes that to about 3 ms; the large pay-insight responses (up to 104 KB) gain more.

**What the stack already does:** FastAPI 0.142 with Pydantic 2.13 serialises a response model through Pydantic's Rust core (`dump_json`), so the JSON encoder is not Python's `json`. `uvloop` and `httptools` are already installed, so uvicorn already uses the fast event loop and HTTP parser. `orjson` is not installed.

**Results** (10,000-employee database, median of 30 runs, in-process):

| Step | Directory page (50 rows, 20 KB) | Outliers view (422 rows, 104 KB) |
| --- | --- | --- |
| The service call (SQL, ORM objects, totals, post-processing) | 8.45 ms | 61.4 ms |
| Building the response items (three model passes per row) | 0.65 ms | not separate |
| Validating against the response model | 0.16 ms | 0.01 ms |
| **`dump_json` (Pydantic, Rust)** | **0.09 ms** | **0.55 ms** |
| `dump_python` + Python `json.dumps` (for comparison) | 0.22 ms | 1.30 ms |

The framework's fixed cost is small too: the server time of `/health` is **0.4 ms**, and of `/departments` (a session, one tiny query, a 224-byte response) **2.3 ms**.

**Verdict: refuted.** Serialisation, building and validating the directory page together take about **0.9 ms of a roughly 12 ms request (8%)**, and the encoder itself 0.09 ms. For the outliers view the encoder is **0.55 ms of 89 ms (0.6%)**. Even a faster encoder than Pydantic's could save at most that much, so `orjson` could not halve anything: the ceiling is a fraction of a millisecond per request. Not adopted; no dependency added and no code changed.

**Where the Python time actually is:** loading the 50 `Employee` objects with their joined relationships (about 3 ms of the directory's 8.45 ms service call, the rest is the database), then about 0.65 ms building items, 0.1 ms writing JSON, and the fixed 0.4 to 2.3 ms of framework and session. For the pay-insight views, the Python is the work in the service (turning rows into result objects), not the response.

**What this suggests:** the one visible Python cost in a directory request is the ORM objects, which the export (E22) already avoided by selecting only the columns it needs; doing the same for the directory page would save about 2 ms of 12 (17%) but changes the shared code that other endpoints use, so it is a candidate rather than a decision.

---

## E24 — Database round trips

On Render the database is a separate server, so every round trip pays network latency that is invisible on one machine (local: about 0.1 ms; the hosted `/departments` request took 4.4 ms against 1.9 ms here, roughly 0.8 ms per round trip). A directory request runs six statements, and the connection pool's `pool_pre_ping` adds a ping, and the transaction ends with a COMMIT or ROLLBACK, each its own round trip. This counts the round trips of each request and measures how request time grows with latency, then removes the ones that are not needed. To make latency measurable on one machine, `python -m scale_lab.e24_round_trips` runs a small TCP proxy between the API and Postgres that counts round trips (a client message after a server reply) and delays every message by a chosen amount, on the 10,000-employee database.

**Hypotheses (written before building the harness):**

1. **Round trips per request:** `/departments` makes 3 (ping, the query, the closing rollback), the **directory page 8** (ping, 6 statements, the commit), the **employee profile about 8**, and the **pay-insight summary about 7** (ping, 5 statements, the commit).
2. **Time grows linearly with round trips:** with 1 ms of round-trip latency added, the directory request takes about **8 ms longer** than with none; with 3 ms, about 24 ms longer.
3. **Four round trips can go without changing a response:** the ping on every connection checkout (it only matters after an idle period), the check that the reporting currency exists, the lookup of the latest exchange rates, and the per-read promotion of due future-dated pay (which can only find something once per day). I expect the directory to fall from **8 to about 4** round trips and, at 3 ms latency, to be about **12 ms faster** of roughly 36 ms.
4. **Nothing visible changes:** every response is identical, and the existing tests pass.

**What changed** (`app/db/session.py`, `app/services/exchange_rates.py`, `app/services/compensation.py`, `app/main.py`; each has a setting that restores the old behaviour):

1. **Ping only after idle** (`DB_PING_IDLE_SECONDS`, default 10): a pooled connection is tested with `SELECT 1` only if it has sat idle that long, instead of on every checkout; a dead one is replaced. Trade-off, covered by a test: after a database restart, a connection used within the window fails **once** (HTTP 503) before the pool replaces it.
2. **Currencies and rates in memory** (`REFERENCE_CACHE_SECONDS`, default 60): a currency seen once is remembered for the life of the process (currencies are only ever added; an unknown code is always checked in the database, so one added a moment ago works at once), and the latest exchange rates are remembered for up to 60 s, forgotten at once by any write through the API and by a rate refresh in the same process. A refresh by *another* process (the daily job) shows up within the 60 s.
3. **Promotion once a day** (`PROMOTE_ONCE_PER_DAY`, default on): the per-read look for future-dated pay that has come due runs until it finds nothing, then not again that UTC day in that process. A record written today is either current at once or dated after today, so nothing new can fall due before midnight. A promotion that moved rows is not trusted (it might be rolled back) until a run finds nothing.

**Results** (10,000-employee database; the API behind the delaying proxy; 15 requests each, median; "before" is the three settings off, "after" the defaults). Round trips are counted at 1 ms of added latency:

| Request | Round trips | Time at 0 ms added | At 1 ms added | At 3 ms added | Milliseconds added per ms of latency |
| --- | --- | --- | --- | --- | --- |
| reference list (/departments) | 3 → 3 | 3.0 → 2.6 ms | 6.3 → 5.1 ms | 13.9 → 8.9 ms | 3.6 → 2.1 |
| directory page | 9 → 5 | 20.0 → 12.2 ms | 33.2 → 19.5 ms | 58.4 → 34.8 ms | 12.8 → 7.5 |
| employee profile | 9 → 5 | 18.0 → 6.9 ms | 26.4 → 13.6 ms | 50.6 → 27.9 ms | 10.9 → 7.0 |
| pay insight: summary | 8 → 4 | 69.3 → 65.2 ms | 81.6 → 68.7 ms | 102.1 → 79.7 ms | 10.9 → 4.8 |

**The last column is a cross-check:** a request that needs *n* sequential round trips slows by *n* ms for every millisecond of latency. The directory page slows by 12.8 ms per ms before, which is more than the 9 trips my counter saw: a trip is sometimes more than one exchange (preparing, then executing), so the counted trips are a lower bound. By the slope the directory page went from about 13 exchanges to 7.5.

**Responses are unchanged:** 19 requests (the reference lists, the directory with filters, sorts and another currency, an unknown currency's error, a profile, the CSV export, the pay-insight views and a conversion) returned **byte-identical** bodies from a server with the three settings off and one with the defaults. The suite passes with the old behaviour (596 tests, the test environment keeps the three settings off) and with 15 new tests for the three mechanisms (611).

**Verdicts:**

1. **Close, but one more than I predicted.** I predicted 3 / 8 / 8 / 7; the counts were 3 (4 at the start) / **9 / 9 / 8**: one more per request than I had listed (the transaction's start is its own exchange).
2. **Confirmed, with a steeper slope.** Time grows linearly with latency, but at about 1.4 ms per counted trip per ms of latency, for the reason above: the directory slowed by 13 ms at 1 ms and 38 ms at 3 ms (I predicted 8 and 24).
3. **Confirmed, and better than predicted.** The directory went from 9 trips to **5** (the four savings) and at 3 ms of latency from 58 ms to **35 ms** (**24 ms faster, 40%**; I predicted 12 ms). The profile, which has the same shape, fell from 51 ms to 28 ms at 3 ms. The pay-insight summary saves the same 4 round trips (**22 ms of 102 ms**) but they are a small part of a 65 ms request.
4. **Confirmed.** Identical bytes, tests green.

**Reading this for Render:** the hosted `/departments` took 4.4 ms against 1.9 ms locally, which suggests the database sits roughly 1 ms away (an inference, not a measurement); at that distance a directory page falls from about 33 ms to about 20 ms of server time and a profile from 26 ms to 14 ms.

**Still to be had** (not done): the directory still makes 3 queries (page, totals, count) that could be one statement with a lateral join for the totals, for about two fewer exchanges, but it changes shared code; the closing commit or rollback is a round trip that autocommit mode would remove at the cost of read consistency across a request's statements.

---

## E25 — The live app on Render, before and after

Everything from E9 to E24 was measured on a laptop, some of it with a proxy that simulated a database on another machine. The original complaint was the hosted app. This measures the deployed app itself, before and after the release that carried E9 to E24 (pull request #14), from one location over the internet. `python -m scale_lab.live_check --url <site> --label <name>`.

**Setup:** Render free tier, the 10,000-employee database. "Before" is `main` at `aec73fb`, "after" is `main` at `7f9dbc6`. Each cell is the median of 8 sequential requests after 2 warm-ups, with a throwaway query parameter so the response cache could not answer them. Server and database time come from the `Server-Timing` header. The "total" column also includes the internet: a trivial request (the reference list) takes about **310 ms** from here whatever the server does, so totals understate the change in server time.

**Predictions (stated before the measurement, in the pull request and the conversation):**

1. The directory search falls from about 480 ms to a few milliseconds (trigram indexes).
2. The CSV export gets faster and arrives compressed.
3. The directory and profile make fewer database round trips and get a little faster.
4. The pay-insight views stay slow, 1 to 3 seconds, because the free-tier CPU is the limit.

**Results** (server time, 10,000 employees):

| Request | Server before → after | Speed-up | Database before → after | Queries | Total before → after |
| --- | --- | --- | --- | --- | --- |
| reference list | 6 → 4 ms | 1.5× | 2 → 2 ms | 1 → 1 | 311 → 310 ms |
| directory page | 26 → 20 ms | 1.3× | 10 → 8 ms | 6 → 3 | 339 → 334 ms |
| directory search | 503 → 207 ms | 2.4× | 483 → 195 ms | 6 → 3 | 818 → 507 ms |
| employee profile | 19 → 11 ms | 1.7× | 8 → 5 ms | 6 → 3 | 357 → 316 ms |
| pay insight: summary | 1,129 → 934 ms | 1.2× | 1,110 → 920 ms | 5 → 2 | 1,465 → 1,247 ms |
| pay insight: outliers | 2,620 → 986 ms | 2.7× | 2,555 → 966 ms | 5 → 2 | 3,109 → 1,509 ms |
| pay insight: histogram | 1,433 → 697 ms | 2.1× | 1,403 → 685 ms | 6 → 2 | 1,737 → 999 ms |
| CSV export | 4,779 → 1,908 ms | 2.5× | 1,274 → 1,006 ms | 15 → 3 | 6,533 → 3,821 ms |

Other findings from the same runs: the first request after the service had been idle took **54 s** (a cold start); after the redeploy it took 0.9 s because the service was already awake, so the wake-up cost is not compared. The JavaScript bundle already arrived gzipped (102 KB before, 104 KB after) because **Render's edge compresses text responses itself**, so the gzip added in E21 changes little there, apart from the CSV export (1,498,307 bytes plain before, 397,139 bytes gzipped after). The bundle now carries `Cache-Control: public, max-age=31536000, immutable`, so a repeat visit does not download it.

**Verdicts:**

1. **Partly refuted.** Search fell from 503 to **207 ms**, not to a few milliseconds. 195 ms of that is still database time, against about 25 ms on a laptop. Two explanations fit and the measurement cannot separate them: the trigram migration skipped its indexes on Render (it is written to do so when `pg_trgm` is not permitted), or the indexes exist but Postgres chooses a sequential scan, which is reasonable on 10,000 rows. The improvement seen could come from the prepared-statement fix alone. **Not verified**: it needs `\d employees` in Render's database shell, to look for `ix_employees_trgm_*`.
2. **Confirmed.** The export's server time fell from 4.8 s to 1.9 s (2.5×), and its download from 1.5 MB to 397 KB; the whole request from 6.5 s to 3.8 s.
3. **Confirmed for the queries, smaller than simulated for the time.** Queries per request fell from 6 to 3, but the directory page gained only 6 ms (26 → 20) and the profile 8 ms. Round trips on Render cost less than the 1 to 3 ms the proxy simulated, so the saving is smaller than E24 predicted for a distant database.
4. **Better than predicted for two views, as predicted for one.** Outliers fell from 2.6 s to **0.99 s (2.7×)** and the histogram from 1.4 s to **0.70 s (2.1×)**, from the query rewrites (E11) and the prepared-statement fix. The summary fell only from 1.13 s to 0.93 s (1.2×). All three are still 0.7 to 1.0 s of pure database time on the shared CPU, about 11 to 14 times slower than the same view on a laptop (65, 89 and 62 ms there for the summary, outliers and histogram); nothing in this release changes that on 10,000 employees, and a repeat request is served from the response cache.

**Limits:** one location, one run, 8 samples per cell; free-tier performance varies from minute to minute. Differences under about 20% (the reference list, the directory page, the summary) are within what that variation could produce.

---

## E26 — Precomputed pay-change events

E16 found the pay-changes report costs a fixed amount, windowing over the whole compensation history, however short the date range, and accepted it because it takes 0.2 to 0.4 s at 10,000 employees on a laptop. E25 then measured the live app: **7.5 s on its own and 15.8 s when the Pay insights page loads its views together**, at 10,000 employees on Render's free tier, about 20 to 40 times slower than the laptop and the only pay-insight view above 2.2 s when run alone. So the report needs fixing in production, not only at a million employees, and the fix E16 named is the right one: store the per-day events once and read them by date range.

**Design** (the tables, the trigger and the maintenance rules are specified in `docs/DATABASE_DESIGN.md`, "Derived data"): `pay_change_events` holds one row per employee per day on which their pay changed (totals before and after, the percentage, why an event is excluded), independent of any filter; a one-row `pay_change_progress` records the day up to which events are stored; a statement-level trigger lists in `pay_change_dirty` any employee who receives a **backdated** record, whose later events are then recomputed; the daily job appends yesterday; today's events are computed live. The report keeps its existing definition and its live computation, which stays as the source for every fill and as the check in the tests.

**Hypotheses (written before building):**

1. **Speed at 10,000 employees:** read from the table, a one-year report takes **under 25 ms** of server time on the laptop (399 ms today) and a one-week report under 10 ms; on Render the one-year report takes **under 0.5 s** against 7.5 s, **at least 15 times faster**.
2. **Same answers:** the report from the table equals the live computation for any date range, filter and combination of corrections, relocations, currency changes and backdated records (a test compares them; the live app's figures must not change).
3. **Maintenance is cheap:** a backdated record is reflected in the next report; ordinary writes (dated today or later) add no work, and the trigger costs under 5% on a 100,000-employee bulk insert (the E13 test).
4. **Cost to build:** the backfill takes under 3 s at 10,000 employees on the laptop and under 3 minutes at 1,000,000; filling one day takes under 1 s at 10,000 and under 3 s at 1,000,000 (unmeasured; it is the E16 restricted-employees case, which should suit a single day).
5. **Size:** about 20 MB at 10,000 employees and about 2 GB at 1,000,000 (an estimate from typical column widths).
6. **At 1,000,000 employees** the one-year report, now an aggregation of about 2 M stored events, takes **under 5 s**, against 43 s.

**Results** (in-process, a fresh session per call; stored = the report read from `pay_change_events` plus the live tail; live = the previous computation; `identical` means the two returned equal reports):

| Range ending today | 10,000 employees: events | live → stored | 1,000,000 employees: events | live → stored |
| --- | --- | --- | --- | --- |
| 1 week | 14 | 169 → 21 ms (8.2×) | 0 | 1.0 s → 0.6 s (1.7×) |
| 1 month | 1,158 | 194 → 21 ms (9.4×) | 109,007 | 26.4 s → 2.1 s (12.4×) |
| 1 quarter | 1,607 | 202 → 24 ms (8.5×) | 146,849 | 26.0 s → 2.3 s (11.2×) |
| 1 year | 19,946 | 399 → 49 ms (8.1×) | 1,986,937 | 51.5 s → 11.2 s (4.6×) |

All eight comparisons were **identical**, and so were the seven new tests' scenarios (a raise, a correction, a promotion, a change outside total pay, a currency change, a relocation, a backdated record, the daily fill, an empty database).

**Maintenance and size:**

| | 10,000 employees | 1,000,000 employees |
| --- | --- | --- |
| Backfill of all history | 4.2 s (116,846 events) | **581 s** (11,773,353 events) |
| Table + indexes | 22 MB (13 + 9) | 2,245 MB (1,340 + 905) |
| Fill one day (daily job) | 0.02 s | 4.4 s |
| Recompute one employee | 11 ms | 47 ms |
| Trigger cost on a bulk insert (copying 50,000 / 100,000 records, rolled back) | 2.26 s vs 2.18 s without (+3.7%), 2,780 employees listed | 5.59 s vs 5.71 s without (within noise), 5,601 employees listed |

The event counts equal the number of (employee, date) pairs I counted in the history before building anything (116,846 and 11,773,353).

**Verdicts:**

1. **Partly refuted at 10,000 employees.** The year report takes **49 ms**, not under 25: 8.1× faster than live (399 ms) but not the 16× I predicted. The shorter ranges are 21 to 24 ms (8.2 to 9.4×). The fixed part, about 20 ms, is the report's other work (the promotion check, the dirty list, today's events computed live, the summary and page queries) and is now the whole cost. **Render, measured after the deploy: confirmed.** Server time for a report ending today (5 requests each, cache bypassed): week 89 ms, month 191 ms, quarter 159 ms, year 596 ms (database time 72, 178, 105, 585 ms; 4 queries), against 7.5 s before: about **40 times faster for a month**, and under the predicted 0.5 s for everything but the year. The year report is the one place the stored read is still visibly slow, because it returns about 20,000 events to summarise. Not measured: the other views and the seven-views-at-once case. Raw numbers: `backend/scale_lab/results/e26_live_changes_report.json`; the rest of the live check (unchanged views) is in `live_check_e26.json`.
2. **Confirmed.** Identical reports everywhere I compared them.
3. **Confirmed.** A backdated record is reflected in the next report (a test; the next report recomputes only that employee, 11 to 47 ms); records dated today or later list nobody; the trigger's cost is +3.7% at 10,000 and not measurable at 1M.
4. **Mostly refuted on cost to build.** The backfill took 4.2 s at 10,000 (predicted under 3 s) and **9.7 minutes** at 1,000,000 (predicted under 3 minutes): it works a year at a time and each year windows all the history before it, so the cost grows with the history. Filling one day took 0.02 s at 10,000 as predicted, but **4.4 s at 1,000,000** (predicted under 3 s) for a day with no events: the time is the fixed work of the fill, mostly scanning the 17.7 M records for the recent-writers recheck, not computing events.
5. **Confirmed.** 22 MB and 2.2 GB (the indexes are 40% of it, more than I assumed).
6. **Refuted for the year at 1,000,000 employees.** The one-year report takes **11.2 s** (live 51.5 s, 4.6× faster), not under 5 s: it reads and joins 2 M stored events. A month or a quarter, the ranges people actually ask for, is **12 times faster: 26 s to 2.1 to 2.3 s**.

**Trade-offs:** 2.2 GB at 1M (or 22 MB at 10k) of derived data to keep; a trigger on the history table; a daily fill; a one-time backfill at bootstrap (10 minutes at 1M); and the invariant "stored rows equal the live computation unless the employee is in the dirty list", which the tests enforce but a new write path that bypassed the history table would break.

**Left:** the fixed 20 ms (0.58 s at 1M) and the one-year range at 1M could be cut by a covering index or a simpler summary query; the fill's recent-writers check could use an index on `created_at`; neither was tried. The live behaviour of the deployed app is measured in the next step.

---

## E27 — A cap on concurrent pay-insight requests

E25 found that the Pay insights page, which asks for seven views at once, took 4 to 16 s on Render while each view alone took 1 to 2 s: on a small server the views share the CPU and all finish late. A cap makes the extra requests wait their turn in the API process, before a database connection is taken, so each running view gets more CPU. `HEAVY_CONCURRENCY` (default 2; `0` turns it off) and `HEAVY_WAIT_SECONDS` (default 60, then a 503 with `Retry-After`) apply to every `/analytics/*` route.

**Method.** `backend/scale_lab/e27_concurrency_cap.py` starts the API with each cap and loads the page like a browser (seven views together), as one user and as three at the same moment, response cache off, 10,000 employees, median of 3 rounds after a warm-up. Run twice: on the laptop as it is, and with the database container limited to **0.5 CPU** (`docker update --cpus 0.5`) to imitate a small server. This is an imitation: Render's real limits and noisy neighbours differ. I wrote no hypothesis before the first run; the expectation (a cap helps only when the CPU is the bottleneck) was formed after the laptop run, which is why the second run exists.

**Results** (time until all seven views of a page have arrived, median):

| Cap | Laptop, 1 user | Laptop, 3 users | 0.5 CPU, 1 user | 0.5 CPU, 3 users |
|---|---|---|---|---|
| off | 93 ms | 203 ms | 948 ms | 3,592 ms |
| 4 | 105 ms | 258 ms | 815 ms | 2,309 ms |
| 2 | 145 ms | 412 ms | 485 ms | 1,394 ms |
| 1 | 298 ms | 833 ms | 484 ms | 1,352 ms |

No request failed in any run. Raw numbers: `e27_concurrency_cap_laptop.json`, `e27_concurrency_cap_half_cpu.json`.

**Reading.** With ten free cores the cap is pure cost (a cap of 2 makes the page 1.6 times slower: the views queue although the CPU is idle). When the database has one half CPU it is a large gain: the three-user case drops from 3.6 s to 1.4 s (2.6 times) and a single user from 0.95 s to 0.49 s. Under the cap the work is the same, so the gain is the CPU lost to contention; a cap of 1 is no better than 2, so 2 is the default. On a quiet server the one first view arrives much sooner (the fastest view: 89 ms at cap 2 against 419 ms uncapped at 0.5 CPU). **Not measured on Render.** The cap is a trade: it helps where the server is small and busy, and costs where it is large and idle.

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
| Pay-insight queries at 1M employees | **Changed** (context query, CTEs) | E11: 1.8–4.8× faster (outliers 39.8 s to 8.3 s); a full pass over per-employee pay (~2.3 s) remains; pay-changes report unchanged at 46 s |
| Stored per-employee totals for pay insights | **Changed in development only** (materialized view, refreshed after writes) | E12: views 1.2–2.3× faster again (cumulative 3.3–5.7×); refresh 3–7 s, 71 MB; staleness of about 16 s after a write |
| Trigram indexes for directory text search | **Changed** (migration `0014`) | E13: searches of 3+ characters 8–180× faster at 1M (rare terms 9–28 ms, was 1.6–2.4 s); +39% bulk-insert time, 161 MB |
| Postgres settings for the local database | **Changed** (`shm_size` 512 MB, `shared_buffers` 1 GB) | E14: 503 errors under concurrent load gone (42 to 0); directory +10-20% throughput; views and `work_mem` unchanged |
| Directory total count (exact `count(*)`) | **Kept exact** | E15: 0.2-0.7% of database time in a realistic mix; pay insights are 98.8% |
| Where database time goes | Recorded | E15: pay-changes report 49%, per-view currency count 11%, the six other pay-insight statements 4-11% each |
| Pay-changes report on narrow date ranges | **Not changed** (restricting the history window refuted) | E16: month 1.4× faster but year 2-3× slower; real fix is a stored change log; accepted for now |
| VACUUM and the visibility map | Recorded; no code change | E17: a 5% update burst made counts 3-6x slower at 1M and autovacuum did not repair it; a 4.4 s manual VACUUM did. Irrelevant at 10k |
| Pay-insight context query (currency count) | **Changed in development** (reads the stored totals) | E18: 3.1x faster unfiltered, 1.9x filtered; views 0.2-0.5 s faster; live query kept for production |
| Covering, partial and BRIN index variants | **Not adopted** | E19: planner ignored both covering indexes; BRIN made a date-range count 3x slower (physical order does not follow date) |
| `max_parallel_workers_per_gather` and `effective_cache_size` | **Defaults kept** | E20: 0 workers made three views 1.5-2.6x slower but gave +8% directory throughput under load; 4 or 8 workers gained only on one view; cache size changed nothing |
| Compression and cache headers | **Changed** (gzip level 5 for responses of 1 KB or more; immutable hashed assets) | E21: JSON 4.7-6x and bundle 3.4x smaller; export 2.8 s to 0.6 s at 4 Mbps but 40 ms slower at 100 Mbps; bug fixed (tiny responses were being gzipped) |
| CSV export | **Changed** (columns only, one totals query, leaner writer) | E22: 10k export 455 ms to 178 ms as an HTTP request, identical file; 1M one-country export 1.7-1.9x faster; a joined-totals first attempt was slower at 1M |
| Serialisation (`orjson`) | **Not adopted** (refuted) | E23: the response encoder is 0.09 ms of a 12 ms directory request and 0.55 ms of 89 ms for the largest view; FastAPI already serialises through Pydantic's Rust core |
| Database round trips | **Changed** (idle-only ping, remembered currencies and rates, promotion once a day) | E24: directory 9 to 5 round trips, 58 to 35 ms at 3 ms of database latency; responses byte-identical; one failed request after a database restart is the cost |
| Live app on Render, before and after the release | Measured | E25: outliers 2.6 s to 1.0 s, histogram 1.4 s to 0.7 s, export 4.8 s to 1.9 s (1.5 MB to 0.4 MB), 6 to 3 queries per request; search 503 to 207 ms (trigram indexes on Render unverified); summary only 1.2x |
| Precomputed pay-change events | **Changed** (production and development; tables `pay_change_events`, `_progress`, `_dirty`, migration `0015`) | E26: report 8x faster at 10,000 employees (399 to 49 ms), 12x for a month at 1M (26 to 2.1 s), 4.6x for a year (51 to 11 s); identical results; 2.2 GB and a 10-minute backfill at 1M; live on Render: 7.5 s to 0.19 s for a month (about 40x), 0.6 s for a year |
| Cap on concurrent pay-insight requests (`HEAVY_CONCURRENCY=2`) | **Changed** (production and development) | E27: with the database limited to 0.5 CPU, the page with three users 3.6 s to 1.4 s (2.6x), one user 0.95 s to 0.49 s; with 10 free cores it is 1.6x slower; Render not yet measured |
