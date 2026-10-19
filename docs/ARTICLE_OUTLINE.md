# Article layout (backend; experiments E1 to E25)

**Working title:** "I loaded 17 million rows into Postgres to see which optimisations actually matter"

**Subtitle:** A salary-management app, 25 measured experiments, and what survived a free-tier server.

**Thesis:** most of the optimisations I expected to matter did not. What did: finding the real bottleneck first, one hidden Postgres behaviour, query shape, and a few indexes. Several of the best-looking ideas were wrong, and the live server disagreed with the laptop in places. The failures are the interesting part, so they stay in.

Written backend-only; the author adds one closing paragraph on the frontend. Numbers come from `docs/PERFORMANCE.md` (experiment ids in brackets).

---

## 1. The question (about 300 words)

- The app is a salary-management system; the brief asks for 10,000 employees. What breaks at 100 times that, and does any of it matter at 10,000?
- Two kinds of scale, kept apart throughout: **data volume** (a million employees) and **request load** (concurrency).
- The data: 1,000,000 employees, 17.7 M history rows (about 18 per employee), 4.3 M "current pay" rows, 8 legal entities, synthetic and seeded.
- The rule I ended up with: **defaults are tuned for 10,000 employees; anything built for a million switches on only in the development environment.**

## 2. How I ran it (about 400 words)

- A hypothesis written before each run; the median of 5 after a warm-up; `EXPLAIN (ANALYZE, BUFFERS)`; one script per experiment in the repo.
- Be open that **twice** the hypothesis came from the first profile, not before it (E11, E16), and that one prediction was made without measuring (serialisation, E23).
- Honest limits: one machine (Apple Silicon, 10 CPUs, Postgres 16 in Docker); E4 to E8 ran on a 4-vCPU Linux box, so absolute times do not compare across experiments; the same views ran **twice as fast** in one session as in another with no code change (E20), so only compare numbers within one experiment.
- Short, honest note that the experiments were run in pairing with an AI coding assistant (the repo has `docs/AI_USAGE.md`).

## 3. Find the bottleneck before touching anything (E9, E15)

- Load test at 1 to 50 clients: directory throughput plateaus at **about 13 req/s from 5 to 10 clients**.
- The CPU split settles it: the **Python server used 13% of one core, Postgres used all ten**.
- So more workers (1 to 4) and a bigger pool (15 to 50 connections) did **nothing**, and made things worse: **42 HTTP 503s** from the container's 64 MB `/dev/shm` (fixed in E14).
- `pg_stat_statements` over a realistic mix (E15): pay insights are **98.8% of database time**; one report alone **49%**; the directory's exact counts only 0.2 to 0.7%. This is why the directory count stayed exact.
- **Lesson:** an average hides it; add capacity only after the per-request work is cheap.

## 4. The surprise: a statement that gets slower the more you use it (E4, E10)

- A search went from about 2 ms to about **4,000 ms** after roughly ten executions on the same connection; later the same effect hit a statement that runs on every read, **1.4 ms to 153 ms**.
- Cause: psycopg prepares a statement after 5 uses, and Postgres then picks a generic plan that cannot use a partial index.
- Fix: one line, `prepare_threshold=None` (single-client directory latency 2 to 3.5 times lower).
- Show how I misread it first: I blamed the exact `count(*)` (E9) and had to correct that in E10.

## 5. Fix the shape of the data and the query

- **Pagination (E2):** `OFFSET` 2.6 s for the last page at 1M; keyset under 1 ms.
- **Current pay table (E3):** 3 to 4 times faster for whole-population queries; the composite index is essential (1.8 ms with it, 2.9 s without).
- **Rewriting the pay-insight queries (E11, E18):** one "context" statement recomputed the whole population just to count currencies (3.5 to 7 s per view); a subquery used twice is computed twice, a CTE once. Outliers **39.8 s to 8.3 s**, others 1.8 to 4.8 times.
- **The CSV export (E22):** 455 ms to 178 ms, identical bytes. The trap: my first, faster-at-10k version was **slower at 1M** (a grouped join totals everyone's pay to export 40,000 people). Check at scale before trusting a win.
- **Bulk loading (E1):** `COPY` 7.9 times faster than row-by-row; and the `lpad` bug that collided employee codes at hire number 1,000,000 (only the 1M run exposed it).
- **Large CSV import (E6):** staging plus set-based SQL; flat memory; a missing `ANALYZE` on the temp table made validation **95 s instead of 11.8 s**; raising `work_mem` made one step 8 times slower and I never found out why (say so).

## 6. Indexes: the one that paid, the ones that did not (E4/E13, E7, E19)

- **Trigram (`pg_trgm`) indexes:** rare searches **1.6 to 2.4 s to 9 to 28 ms** (85 to 180 times); price: 161 MB, 21 s to build, **+39% bulk-insert time**. A test guards the index expressions.
- **Index write cost (E7):** going from 3 to 11 indexes cost only 3.7%; **no indexes made the load 4.4 times slower** because the seed reads back what it writes.
- **Covering, partial and BRIN (E19):** the planner ignored both covering indexes; **BRIN made a date-range query 3 times slower** because the table's physical order does not follow the date.

## 7. Pre-computation and its price (E5, E12, E18)

- Materialised view of each employee's total pay: 4.5 to 11 times faster against the unrewritten queries (E5), but **only 1.2 to 2.3 times on top of the rewrites** (E12). Say why the gain shrank.
- The price: staleness (about 16 s behind a change), a background refresh thread, a function with elevated privileges, a second copy of a number. "Is this over-engineering?" is a fair question.
- The decision that shaped the project: it works at 10,000 without it, so it is **development-only** (`ENVIRONMENT`). Defaults for the submission stay simple.

## 8. Ideas that looked good and were wrong (E8, E16, E23, E17)

- **Partitioning (E8):** date reports 2.2 to 3.7 times faster, per-employee lookups 1.6 to 3.5 times slower, and the primary key had to change. Rejected.
- **Pay-changes report on a narrow range (E16):** restricting the work to the affected employees was 1.4 times faster for a month and **2 to 3 times slower for a year**. The real fix (a stored change log) costs more than the report is worth.
- **`orjson` (E23):** my prediction was it would halve the Python time. The encoder is **0.09 ms of a 12 ms request**; FastAPI already encodes in Rust.
- **VACUUM (E17):** a quiet failure, not a speed-up: a 5% scattered update made counts 3 to 6 times slower and autovacuum did not repair it for 3 minutes; a 4.4 s manual VACUUM did.

## 9. Configuration is not optimisation, until it is (E14, E20)

- `shm_size` 64 MB to 512 MB: 42 errors to none; `shared_buffers` 1 GB: directory +10 to 20%.
- `work_mem`, parallel workers and `effective_cache_size`: defaults kept, with the trade-off that more workers help one heavy query and cost a busy server.

## 10. The application layer: round trips and bytes (E24, E21)

- **Round trips:** a tiny TCP proxy that counts round trips and adds latency, to make a remote database visible on one machine. A directory page needed **9 round trips**; ping-only-when-idle, remembered currencies and rates, and "look for due pay once a day" cut it to **5** (58 ms to 35 ms at 3 ms of latency). Cost: one failed request after a database restart. Responses byte-identical.
- **Compression and caching (E21):** JSON 4.7 to 6 times smaller, the bundle 3.4; the export's download 2.8 s to 0.6 s on a 4 Mbps link, but **40 ms slower on a 100 Mbps link**. A bug found on the way: tiny responses were being gzipped into bigger ones.

## 11. Reality check on a free-tier server (E25)

- Same 10,000 employees, live: outliers **2.6 s to 1.0 s**, histogram 1.4 s to 0.7 s, export 4.8 s to 1.9 s (1.5 MB to 0.4 MB), 6 to 3 queries per request.
- What did not carry over: the directory gained only 6 ms (round trips are cheaper on Render than my simulation); the summary only 1.2 times; Render's edge already gzips text; the free-tier CPU makes the heavy views about **11 to 14 times slower than a laptop**.
- Checked afterwards: the trigram indexes do exist on Render, so that explanation is out. What I still do not know: why search is 195 ms of database time there (a sequential scan of 10,000 rows or a slow CPU; `EXPLAIN` on Render would say). Say it plainly.
- Cold start: 54 s on the first request after idle.

## 12. Scorecard

- One table from `PERFORMANCE.md`: decision, status (changed, kept, rejected), evidence. Group it as **shipped for everyone / development only / measured and rejected**.

## 13. What I would tell someone starting

- Measure first; look at the tail and at CPU, not averages.
- Be suspicious of a win at 10,000: re-check at the size you care about (the export).
- Know your database's hidden behaviours (prepared statements, `/dev/shm`, autovacuum thresholds).
- An optimisation that needs a background thread and a privileged function is a cost, not a free win.
- Make a cheap thing cheaper only if it is on the critical path (serialisation was not).
- Write down the failures; keep the predictions that were wrong.
- Use the live server as the final judge; it disagreed with the laptop in four places.

## 14. Closing

- **Frontend paragraph:** placeholder, written by the author.
- Limits and what is left (a stored change log, a sharding study as a capstone, the unanswered trigram question).
- Link to the repo, the scripts and `docs/PERFORMANCE.md`.

---

## Notes before writing

- Sources: every figure above is in `docs/PERFORMANCE.md`; keep the experiment ids in the text so readers can check.
- Do not mix absolute times across experiments (E1 to E3 and E9 onward on the Mac, E4 to E8 on the Linux box).
- Two figures to word carefully: the Render-versus-laptop ratio (11 to 14 times, for the three heavy views) and the unexplained `work_mem` result.
- Length target: about 4,000 to 5,000 words, with 4 to 6 small tables and 2 or 3 short code snippets (`prepare_threshold=None`, the trigram index, the CTE point).

## Decisions for the author

1. Title and tone: keep "I loaded 17 million rows…", or something less clickbait?
2. Disclose the AI pairing in the method section (suggested), in a closing note, or not at all?
3. Include the parts about the "over-engineering" discussion and the environment rusle (section 7), or keep the article purely technical?
4. Single long post, or split into two (data volume; application layer and the live app)?
