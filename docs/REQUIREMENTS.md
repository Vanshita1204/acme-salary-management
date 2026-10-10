# Requirements — ACME Salary Management

> This is the one-page requirements document requested in the brief.
> The detailed specification that the build and tests are written against is in [`SPECIFICATION.md`](SPECIFICATION.md).

---

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
5. **Pay insights:** dashboards and filters answering the questions an HR Manager actually asks (`SPECIFICATION.md` §5).
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
| Background job infrastructure | Not needed at 10,000 employees. Required for imports and exports at 1,000,000 scale (`SPECIFICATION.md` §8). |
| Per-company compensation type catalogs | The compensation type catalog is universal (shared by every ACME entity), not per-company. Simpler for v1; company-specific catalogs are a future option (`SPECIFICATION.md` §10). |
