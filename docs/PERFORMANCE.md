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
- `population()` reads it when `ANALYTICS_TOTALS_VIEW` is on (default); off falls back to the live aggregate. Tests run with it off (they check the live semantics) and one test compares the two.
- Refreshed: at bootstrap, at the end of the daily job (promotion changes pay without an API write), and by a background thread in the API **10 s after the latest write** (`TOTALS_REFRESH_DELAY`), so a burst of writes costs one refresh; a read that promotes a due future-dated change also marks it stale. The response cache is emptied after each refresh.
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
3. **Confirmed, with a design to bound it.** Staleness is about the refresh delay plus the refresh itself (10 s + ~6 s at 1M). Pay changes made outside the API and not through a promotion would wait for the daily run. The directory is *not* affected: it still computes the totals of the 50 people on a page live, and the response cache is emptied on each refresh.

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
| Stored per-employee totals for pay insights | **Changed** (materialized view, refreshed after writes) | E12: views 1.2–2.3× faster again (cumulative 3.3–5.7×); refresh 3–7 s, 71 MB; staleness of about 16 s after a write |
| Trigram indexes for directory text search | **Changed** (migration `0014`) | E13: searches of 3+ characters 8–180× faster at 1M (rare terms 9–28 ms, was 1.6–2.4 s); +39% bulk-insert time, 161 MB |
| Postgres settings for the local database | **Changed** (`shm_size` 512 MB, `shared_buffers` 1 GB) | E14: 503 errors under concurrent load gone (42 to 0); directory +10-20% throughput; views and `work_mem` unchanged |
| Directory total count (exact `count(*)`) | **Kept exact** | E15: 0.2-0.7% of database time in a realistic mix; pay insights are 98.8% |
| Where database time goes | Recorded | E15: pay-changes report 49%, per-view currency count 11%, the six other pay-insight statements 4-11% each |
