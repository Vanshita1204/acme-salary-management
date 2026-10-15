# Performance — Scale Lab Results

Results of the Scale Lab experiments (`docs/SCALE_LAB.md`): E1–E3 in Phase 1, E4–E8 in Phase 16, E9 on the hosted instance. Run at **S = 10,000** and **M = 1,000,000** employees. Raw numbers and full `EXPLAIN (ANALYZE, BUFFERS)` plans are in `backend/scale_lab/results/*.json`; `python -m scale_lab.report` regenerates the tables.

All results are synthetic data on one local machine. They show how queries and the data model behave at volume, not production traffic or concurrency.

## Setup

- **Machine (E1–E3, directory and Phase 14 checks):** Apple Silicon Mac, 10 cores, 16 GB RAM, SSD.
- **Machine (E4–E8):** Linux x86-64, 4 vCPU, 16 GB RAM. Absolute times are not comparable with E1–E3; compare columns within a table.
- **Database:** PostgreSQL 16 in Docker, default configuration (`shared_buffers` 160 MB, `work_mem` 4 MB).
- **Data:** `python -m app.seed.employees --count N --as-of 2026-10-01`, fixed seed 1204. The seed generates full history, so ~18 compensation records per employee.

| Dataset | Employees | Compensation records | Current-compensation rows | On disk (records) |
| --- | --- | --- | --- | --- |
| S | 10,000 | 178,527 | 43,493 | 30 MB |
| M | 1,000,000 | 17,763,707 | 4,341,442 | 2.9 GB |

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

## E9 — Pay insights on the hosted instance (Render free tier)

**Observation:** the Pay insights page was slow on the live app (a 10,000-employee database) but not on a laptop with the same data. Not part of the original plan; found by using the deployed app.

**Instrumentation added first.** Every API response now carries a `Server-Timing` header: total server time (request received to response ready), database time with the number of queries and the slowest one, and whether the answer came from the cache. The Pay insights page shows these next to the browser's round trip, so server time, database time and network can be told apart.

**Measured.** Server time per view, first load (cache miss):

| View | Laptop | Render, one request at a time | Render, all views at once |
| --- | --- | --- | --- |
| Summary | 197 ms | 2.8 s | 4.7–15.8 s |
| Pay by group (stats) | 79 ms | 1.8 s | 9.5–10.4 s |
| Same role across countries | 110 ms | 2.1 s | 6.1–7.1 s |
| Pay distribution (histogram) | 104 ms | 1.7 s | 9.9–11.4 s |
| Outliers | 148 ms | 3.2 s | 10.7–12.7 s |
| What pay is made of (composition) | 100 ms | 1.4 s | 8.6–12.7 s |
| Pay changes | not measured | 8.0 s | up to 21.7 s |

The laptop figures include 5–6 queries per view, 70–130 ms of it in the database. Other endpoints on Render are fine: `GET /departments` 5 ms, `GET /employees?limit=50` 40–120 ms. Run on 2026-10-15 against the live instance; the "all at once" column is the spread over several runs and the page's own timing table.

**What was ruled out.** Missing statistics: `pg_stat_user_tables` showed all three large tables analysed by autovacuum (`last_autoanalyze` set; 10,000 / 176,719 / 43,474 live rows for `employees` / `compensation_records` / `current_compensation`).

**What is not known.** Why the same queries are 10–20× slower on Render. The request times add up almost linearly when sent together (each view finishes a little later than the last), which points to a shared, CPU-limited instance, but it is **not confirmed**: no `EXPLAIN (ANALYZE, BUFFERS)` has been taken on the hosted database, and the database-time column added above had not yet been read from the live app when this was written.

**Changes made, and their effect:**

| Change | Effect |
| --- | --- |
| In-process response cache for `/analytics/*` GETs, 5-minute limit, emptied by any write through the API, bounded to 500 entries | Locally, summary 191 ms on a miss and about 0.1 ms on a hit. On Render, a cached view showed 0 ms server time. First loads are unchanged. |
| Views below the fold load when scrolled into view | Intended to avoid seven heavy queries at once; effect on Render not yet measured |
| Summary computed in one statement (population computed once, shared by the overall figures and three breakdowns) | No measurable change locally (about 195 ms before and after); effect on Render not yet measured |

**Trade-offs of the cache:**

- Staleness: changes made outside the API (the daily exchange-rate refresh, future-dated pay taking effect) show up after up to 5 minutes.
- It lives in one process: correct for the single hosted instance, wrong for several workers, which would each hold their own copy or need a shared cache such as Redis.
- It only helps repeat requests; the first load of each filter combination is as slow as before.

**Conclusion so far:** the directory meets its 500 ms target on the hosted instance; the whole-population pay insights do not on the free tier. The cache fixes repeat visits, not first visits. If the database column shows the time is in the queries, the next step is pre-aggregation (E5's materialized view was 4.5–11× faster at 1M employees); if it is Python work or waiting, connection-pool and concurrency settings.

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
| Pay insights on the hosted free tier | Limit recorded, partly mitigated | E9: 1.4–8 s per view on Render vs 80–200 ms locally; cache makes repeats instant, first loads unchanged; cause not yet confirmed |
