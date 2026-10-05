# Requirements — ACME Salary Management

> **Part 1** is the one-page summary requested in the brief.
> **Part 2** is the detailed specification that the build and tests are written against.

---

# Part 1 — One-Page Summary

## Problem
ACME's HR team manages compensation for ~10,000 employees across multiple countries in Excel. This causes:
- **No history or audit trail:** overwriting a cell erases the previous salary, and who changed it.
- **Manual, error-prone analysis:** every question about pay needs hand-built formulas and pivots.
- **Currency confusion:** salaries in different currencies are hard to compare or total correctly.
- **Weak data integrity:** duplicate employees, invalid values, and inconsistent department or title names go uncaught.

## Goal
A web application where the HR Manager can maintain accurate, auditable compensation data and answer questions about how ACME pays people in seconds instead of hours.

## User
A single HR Manager (confirmed with Incubyte). Fluent in spreadsheets, not in SQL. Needs speed on routine edits and trustworthy numbers on aggregate questions.

## In Scope
1. **Employee management:** create, view, edit, and terminate employees; search, filter, and paginate. Built for 10,000 employees, designed to scale to 1,000,000 without redesign.
2. **Compensation components:** an open-ended, centrally-administered catalog of compensation types (base pay, variable pay, equity, allowances, bonus, etc.), each with its own payment period (monthly, quarterly, annual, …), recorded in the employee's local currency.
3. **Compensation history:** every change is a new dated, reasoned, append-only record, including changes of country.
4. **Multi-currency reporting:** amounts stored in local currency; analytics converted to a selectable reporting currency (default USD) using live exchange rates refreshed daily.
5. **Pay insights:** dashboards and filters answering the questions an HR Manager actually asks (Part 2, §5).
6. **CSV import:** bulk-onboard employees from a spreadsheet, validated before anything is saved. This is the migration path off Excel.
7. **CSV export:** of any filtered employee view, for Finance and audit handoff.

## Deliberately Left Out
| Feature | Reasoning |
|---|---|
| Authentication and RBAC | Confirmed not required. One user, so "changed by" on history is a label, not a verified identity. First addition if more HR users are onboarded. |
| Natural-language / AI Q&A | Confirmed not required. Structured analytics produce verifiable, reproducible numbers, which matters more for pay data than conversational convenience. |
| Payroll, tax, deductions, payslips | A separate system with per-country legal correctness requirements. This tool is the compensation record of truth, not a payroll engine. |
| HR-defined salary bands | Band management is its own workflow. Peer medians (same role, level, country) serve as the comparison baseline. |
| Updating existing employees via import | Import creates new employees only. Bulk updates (e.g. an annual revision cycle) need diff previews and conflict rules; v2. |
| Historical exchange rates in analytics | Analytics use the latest rates for all figures. Reporting "what did this cost at the time" needs rate history per date; v2. |
| Approval workflows for pay changes | Assumes the HR Manager is the approver. Needed once others propose changes. |
| Org hierarchy, performance reviews, benefits | Adjacent HR domains, not compensation management. |
| Concurrent-edit conflict handling | Unnecessary with one user; optimistic locking would ship with multi-user support. |
| Background job infrastructure | Not needed at 10,000 employees. Required for imports and exports at 1,000,000 scale (Part 2, §8). |
| Per-company compensation type catalogs | The compensation type catalog is universal (shared by every ACME entity), not per-company. Simpler for v1; company-specific catalogs are a future option (§10). |

---

# Part 2 — Detailed Specification

## 1. Success Criteria
- HR can find any employee and see their full compensation history in under 10 seconds.
- Every question in §5 is answerable from the UI without exporting to Excel.
- No compensation change can silently overwrite a previous value.
- Invalid import rows are reported with row number and reason; no partial imports.
- List, search, and filter responses on 10,000 employees return in under 500 ms.
- Analytics keep working when the exchange-rate provider is unavailable.

## 2. Assumptions
- One HR Manager uses the system at a time.
- An employee has one current country and one current pay currency, tracked independently — the two usually match (paid in the local currency) but don't have to (e.g. based in the UAE, paid in USD). Either can change; see §4, FR-7.
- Each compensation type records an amount for its own payment period (monthly, quarterly, annual, …); totals are annualized for reporting (§3).
- Seeded data stands in for ACME's existing records.

## 3. Data Model
> Full table-by-table schema, constraints, and the rationale behind them live in `docs/DATABASE_DESIGN.md`. This section is the model at the level the rest of this spec is written against.

**Company:** id, name. One row per ACME legal entity; every employee belongs to exactly one.

**Employee:** employee code (unique, system-generated), company, first and last name, email (unique), department, job title, job level, current country, current pay currency (independent of country), employment status (active / on leave / terminated), hire date, termination date (set exactly when status becomes terminated).

**Compensation type:** a catalog entry describing one kind of pay component — category (e.g. fixed, variable, equity, allowance, bonus), subtype (e.g. housing, quarterly), payment period in months (1 = monthly, 12 = annual, etc.), whether it's the one base-pay type, and a display name. One universal catalog, shared by every employee at every company — not per-company, not per-employee.

**Change reason:** a reason valid for one specific compensation type (e.g. "new hire" for base pay, "quarterly payout" for a quarterly bonus). Every type also has a "correction" reason.

**Compensation record:** employee, compensation type, effective date, country, currency, amount (for that type's payment period), change reason, note, changed by, created at.

Each record stores its own country and currency, so a relocation or currency change doesn't retroactively alter earlier records.

**Current compensation:** one row per (employee, compensation type), pointing to that type's latest compensation record. Kept so that the directory and analytics never recompute "latest record per employee per type" on every request.

**Exchange rate:** currency, rate against USD, rate date, source, fetched at.

Derived values (never stored): a compensation type's annualized amount = amount × 12 ÷ payment period (months); total compensation = the sum of every one of an employee's current compensation types' annualized amounts.

## 4. Functional Requirements

### FR-1 Employee Directory
- Table showing code, name, department, title, level, country, status, and current total compensation in local and reporting currency.
- Free-text search on name, email, and employee code.
- Filters on department, country, job title, level, and status, combinable.
- Sortable by name, hire date, and compensation.
- Cursor (keyset) pagination with next and previous navigation.

### FR-2 Employee Profile
- All employee details and the current compensation breakdown in local and reporting currency.
- Full compensation history in reverse chronological order, with change reason, note, changed by, and percentage change from the previous record.

### FR-3 Create and Edit Employees
- Creating an employee requires an initial compensation record for the base-pay type (reason: new hire); other compensation types are added afterward through FR-4.
- Editing updates employee details only; compensation changes go through FR-4.
- Terminating sets status to terminated and records a termination date; records are never hard-deleted.

### FR-4 Record a Compensation Change
- Adds a new compensation record for one compensation type; previous records are never modified.
- The change reason must be one valid for that compensation type (§3) — reasons aren't shared across types except "correction," which every type has.
- Mistakes are fixed with a new record of reason "correction", preserving the audit trail.
- Future effective dates are allowed (e.g. an approved raise from next month) and become current on that date.

### FR-5 CSV Import
- Downloadable template with one row per employee: name, email, company, department, title, level, country, hire date, currency, base pay amount. Only the base-pay compensation type is set at import; other types (bonus, allowances, equity, etc.) are added afterward through FR-4.
- Two steps: **validate and preview**, then **confirm**. The whole file is validated first; row-level errors are shown with row number, column, and reason.
- **All-or-nothing:** if any row is invalid, nothing is saved. HR fixes the file and re-uploads. This avoids a half-imported state that is hard to reconcile.
- Rejected when: a required field is missing; the email is malformed, already exists, or is duplicated within the file; the company or currency is unrecognized; an amount is negative or the base pay amount is zero; a date is invalid.
- Maximum 10,000 rows per file.

### FR-6 CSV Export
- Exports the currently filtered directory view, including current compensation in local and reporting currency.

### FR-7 Country and Currency Changes
- **Country change:** updates the employee's current country directly, and is recorded as a new compensation record for the base-pay type with reason "relocation" (same amount and currency, unless the currency is also changing) — this is what makes the relocation itself a dated, reasoned entry in the compensation history (Part 1, Scope #3), even when no amount changes.
- **Currency change:** independent of country — an employee's pay currency can change without relocating. Requires a new compensation record for *every* compensation type the employee currently has, submitted together as one HR-reviewed step, each with an HR-provided amount in the new currency. There is no automatic conversion (see `docs/DATABASE_DESIGN.md` for why); the employee's currency only updates once every type has been re-recorded.
- Earlier records keep their original country and currency.
- *Planned separately:* how percentage change and increase reports are calculated across two currencies.

### FR-8 Exchange Rates
- Rates are fetched from an external provider once a day and stored.
- If a fetch fails, the latest stored rates remain in use and the failure is logged.
- Every analytics view displays the date of the rates it used.
- The reporting currency is selectable in the UI; conversions between non-USD currencies go through the stored USD rates.

## 5. Pay Insights

All analytics include **active employees only** and use **current compensation converted to the selected reporting currency**. Every view respects the directory filters.

| Question HR asks | View |
|---|---|
| What does ACME spend on compensation, overall and by country and department? | Summary cards and cost breakdown |
| What are average, median, minimum, and maximum pay by department, country, title, and level? | Grouped statistics table |
| How does pay for the same role and level differ across countries? | Role-by-country comparison |
| What does the pay distribution look like? | Histogram |
| Who is paid well below or above their peers? | Outlier list |
| Who had a compensation change in a given period, and what was the average increase? | Change report with date range |
| What share of compensation falls into each compensation category (base pay, bonus, allowances, equity, …)? | Composition breakdown |

**Definitions**
- **Peer group:** same job title, level, and country.
- **Outlier:** total compensation below 80% or above 120% of the peer-group median. Only peer groups with at least 5 employees are evaluated, so small groups don't produce misleading flags.
- **Increase:** percentage change in total compensation from the previous record, in the same currency. Corrections are excluded from average-increase calculations; relocations are handled per FR-7.

## 6. Business Rules
- The base-pay compensation type's amount must be greater than zero; every other type's amount must be zero or greater.
- A compensation record's change reason must be valid for its compensation type.
- A compensation type's payment period must be a positive number of months.
- A new compensation record must use the employee's current pay currency; changing an employee's currency re-records every current compensation type at once (§4, FR-7).
- A compensation effective date cannot be earlier than the employee's hire date.
- Employee email and code are unique.
- Currency must be one of the supported currencies.
- Terminated employees remain searchable but are excluded from analytics.

## 7. Non-Functional Requirements
- **Performance:** keyset pagination, indexes on every filtered column, and SQL-side aggregation keep directory responses under 500 ms at 10,000 employees.
- **Data integrity:** append-only compensation history; database constraints back the business rules rather than relying on UI validation alone.
- **Resilience:** the app never depends on the exchange-rate provider being available at request time.
- **Seed data:** a deterministic seed script (fixed random seed) generates 10,000 employees with country-appropriate currencies, salary ranges by role and level, and realistic compensation histories.
- **Testability:** compensation, currency, import validation, and analytics logic live in pure functions with no database or network access, so unit tests are fast and deterministic. Tests use fixed exchange rates and never call the live provider. Database queries are tested separately against Postgres.
- **Usability:** clear validation messages, consistent currency formatting, and no action that loses data without confirmation.

## 8. Scalability: 10,000 to 1,000,000 Employees

| Concern | At 10,000 (built) | At 1,000,000 (path) |
|---|---|---|
| Pagination | Keyset pagination | Same; no change needed |
| Result counts | Exact counts | Approximate counts, or counts only when filters change |
| Search | Indexed prefix search | Postgres trigram (GIN) indexes for substring search |
| Analytics | SQL aggregation on the current-compensation table | Same, plus cached or pre-aggregated results refreshed when data changes |
| Import and export | Synchronous request | Background jobs with progress tracking and streamed files |
| Seeding | Batched inserts | Bulk load (`COPY`) |

These choices are tested at 1,000,000 and 10,000,000 employees on synthetic data; see `docs/SCALE_LAB.md` for the benchmark plan and `docs/PERFORMANCE.md` for results.

## 9. Tech Stack
- **Backend:** Python, FastAPI, SQLAlchemy
- **Database:** PostgreSQL (SQL-side median via `percentile_cont`, trigram search, and a realistic scaling path)
- **Frontend:** React (Vite) with a component library
- **Testing:** pytest
- **Deployment:** publicly accessible hosted instance with managed Postgres, seeded on first run

## 10. Future Roadmap
1. Authentication and role-based access for multiple HR users
2. Cross-currency increase reporting for relocations
3. Bulk compensation updates via import (annual revision cycles)
4. Historical exchange rates for point-in-time cost reporting
5. HR-configurable salary bands and compa-ratio reporting
6. Approval workflows for compensation changes
7. Per-company compensation type catalogs, if ACME entities' pay structures diverge enough to need it
