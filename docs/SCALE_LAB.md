# Scale Lab — Benchmark Plan

## Purpose
The deployed application serves 10,000 employees, as the brief requires. This lab runs the same schema and queries locally at 10x, 100x, and 1,000x that size to find where each design choice breaks, why, and what fixes it.

All results come from **synthetic data on a single local machine**. They demonstrate query and data-modelling behaviour at volume, not production traffic or concurrency.

## Setup
- **Database:** PostgreSQL in Docker, same version as the deployed instance, default configuration unless an experiment states otherwise.
- **Data:** the application's seed script with a size flag, e.g. `python -m seed --count 1000000`. Fixed random seed, so every run produces identical data.
- **Hardware:** CPU, RAM, disk type, and Postgres settings recorded alongside every result.

| Dataset | Employees | Compensation records (~3 per employee) |
|---|---|---|
| S | 10,000 | ~30,000 |
| M | 1,000,000 | ~3,000,000 |
| L | 10,000,000 | ~30,000,000 |

## Method
For every experiment:
1. State the hypothesis before running anything.
2. Run `ANALYZE` after loading data, so the planner has current statistics.
3. Execute each query 5 times after one warm-up run; report the median.
4. Capture `EXPLAIN (ANALYZE, BUFFERS)` for the baseline and the fix.
5. Record the result in `docs/PERFORMANCE.md`: hypothesis, numbers at each size, query plan summary, conclusion.

A result that disproves the hypothesis is recorded just the same.

## Experiments

### Phase 1 — before submission (directly improves the application)

**E1. Bulk loading**
Compare ORM row-by-row inserts, batched inserts, and `COPY`.
Measure: total load time and rows per second at S, M, L.
Outcome: the method the seed script uses.

**E2. Pagination**
Compare `OFFSET` pagination with keyset (cursor) pagination, on the first page, a middle page, and the last page.
Measure: latency per page position at each size.
Outcome: confirms or refutes the keyset decision in the requirements.

**E3. Current compensation per employee**
Compare `DISTINCT ON`, a window function (`ROW_NUMBER`), and a denormalised current-compensation table, each with and without a composite index on (employee, effective date).
Measure: latency of a directory page and of a full-table aggregation.
Outcome: confirms or refutes the current-compensation table in the data model.

### Phase 2 — after submission

**E4. Search**
Compare `ILIKE '%term%'` without an index, a B-tree prefix search, and a `pg_trgm` GIN index.
Measure: latency for short and long search terms; index size and build time.

**E5. Analytics aggregation**
Compare live aggregation (`percentile_cont` for median) with a materialised view.
Measure: query latency, and the cost of refreshing the view after a batch of changes.

**E6. Large CSV import**
`COPY` into a staging table, validate with SQL, and insert all-or-nothing in one transaction.
Measure: end-to-end time for 100,000 and 1,000,000 rows; peak memory of the application process.

**E7. Index write cost**
Load the same data with zero, essential, and all indexes.
Measure: load time and per-row insert latency against read latency gained.

**E8. Partitioning (L only)**
Range-partition compensation records by effective date.
Measure: date-range report latency with and without partition pruning, and the impact on the current-compensation query.

## Deliverable
`docs/PERFORMANCE.md`: one entry per experiment, plus a summary of which decisions in the application each result confirmed or changed.
