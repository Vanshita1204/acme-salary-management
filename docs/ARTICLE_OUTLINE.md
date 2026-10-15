# Article layout (data-volume work so far)

**Working title:** "I loaded 17 million rows into Postgres to see which optimisations actually matter"

Written around the surprises, because the hypotheses that failed read better than the ones that held. Numbers come from `docs/PERFORMANCE.md`.

## 1. Hook and setup (~300 words)
- The app is a salary-management system whose brief asks for 10,000 employees. The question is what breaks at 100× that size.
- The data: 1M employees and 17.8M compensation records, with full history (about 18 records per employee).
- Method: state the hypothesis first, `ANALYZE`, one warm-up run, the median of 5, and `EXPLAIN (ANALYZE, BUFFERS)` for every result.
- Honesty note: one machine, synthetic data, no concurrency yet.

## 2. Measure before you tune
- The first benchmark (directory endpoint) showed p95 of 42 ms at 10k and 3,499 ms at 1M.
- Only 2 of 15 query shapes were slow, which told us where to look. The lesson is that an average hides it, so look at the slow tail.

## 3. Query and data-shape wins
- **Pagination (E2).** `OFFSET` reached 2.6 s for the last page, and keyset stayed under 1 ms.
- **Current compensation (E3).**
  - Deriving it from history was 3–4× slower for whole-population queries than a denormalised table.
  - The composite index was essential: a page took 1.8 ms with it and 2.9 s without.
  - The trade-off is that the table duplicates data, so it needs upkeep.
- **Search (E4).** The trigram GIN index took rare-term searches from 2–4 s to 0.4–12 ms.

## 4. The part that surprised us (strongest section)
- **Prepared statements.** The same indexed search went from about 2 ms to about 4,000 ms after roughly 10 executions, because Postgres switched to a generic plan. The fix is per-term plans.
- **Missing `ANALYZE`.** Import validation took 95 s without `ANALYZE` on the temp table and 11.8 s with it.
- **`work_mem`.** Raising it made validation 8× slower, and the cause isn't found yet. Be honest about this one.
- **No indexes was 4.4× slower to load**, not faster, because the seed reads back what it just wrote.
- **Employee-code bug.** `lpad` truncated codes, so hire #1,000,000 collided with #100,000. Only the 1M run exposed it.

## 5. Writes and bulk work
- **Bulk loading (E1).** `COPY` was 7.9× faster than row-by-row inserts and 1.6–1.7× faster than batched inserts, and throughput stayed linear to 1M.
- **CSV import (E6).** Staging plus set-based SQL was only 1.8–2.5× faster, but memory stayed flat at about 118 MB for 1M rows. All-or-nothing validation rejected a bad file in 14.8 s with nothing written.
- **Index write cost (E7).** Going from 3 to 11 indexes cost only 3.7% on load time.

## 6. Pre-computation and its price
- **Materialised view (E5).** It was 4.5–11× faster but still took 0.56–0.91 s for whole-population views.
- **Trade-offs.** The view goes stale when exchange rates change, and a refresh costs 5–9 s regardless of how much changed.
- **Takeaway.** A median over 862k rows is itself the cost, so caching alone won't fix everything.

## 7. What we rejected, and why
- **Partitioning (E8).** Date-range reports were 2.2–3.7× faster, but per-employee lookups were 1.6–3.5× slower and the primary key had to change. Rejected for now.
- **Principle:** adopt an optimisation because it wins on your actual access pattern, not because it won a benchmark.

## 8. Scorecard table
The summary table from `PERFORMANCE.md`: decision, status (confirmed, changed, rejected) and evidence.

## 9. Request-load scaling (placeholder for upcoming work)
Connection pooling, workers, caching, rate limiting, read replicas, and the layered "cheap and safe first, expensive last" framing.

## 10. Closing
- A short frontend paragraph (written separately).
- Limits and what's next.
- Takeaway: measure, fix the data shape, then add infrastructure.

## Notes on the source numbers
- E1–E3 ran on a Mac and E4–E8 on a Linux box, so say not to compare absolute times across experiments.
- `PERFORMANCE.md` has a typo in the dataset table ("4,3 41,442") to fix before quoting it.
- The `work_mem` result has no explanation yet. Either investigate it or label it clearly as unexplained.
