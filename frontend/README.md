# ACME Salary Management: frontend

React + TypeScript + Vite. No UI framework: plain CSS with light and dark themes.

## Run it

```bash
# 1. the API (from backend/): needs Postgres, migrations and the seeds
uvicorn app.main:app --port 8000

# 2. the app (from frontend/)
npm install
npm run dev          # http://localhost:5173
```

In development the app calls `/api/...` and Vite forwards it to the backend with the
prefix removed, so the browser sees a single origin and no CORS setup is needed. Point
it at another backend with `BACKEND_URL=http://host:port npm run dev`. For a production
build, set `VITE_API_URL` to wherever the API is served (see Phase 15 in
`docs/IMPLEMENTATION_PLAN.md`).

| Command | What it does |
|---|---|
| `npm run dev` | dev server with hot reload |
| `npm run build` | type-check, then build to `dist/` (~100 kB gzipped JS) |
| `npm test` | Vitest unit and component tests (mocked API, no backend needed) |
| `npm run typecheck` | TypeScript only |

## Screens

| Route | Requirement |
|---|---|
| `/` Employees | FR-1: search, combinable filters, sort, keyset paging, CSV export of the exact view |
| `/employees/:id` | FR-2: details, current breakdown, history with % change; FR-3 edit and terminate; FR-4 record a pay change; FR-7 relocate and change currency |
| `/employees/new` | FR-3: create with starting base pay |
| `/import` | FR-5: upload → validation report → confirm |
| `/analytics` | §5: spend, pay by group, same role across countries, distribution, outliers, composition, pay changes |
| `/reference` | Compensation types, change reasons, departments, job titles and levels, each with an add form |

The reporting currency (top right) applies everywhere; every figure that was converted
says which exchange-rate date it used. "Acting as" is recorded as `changed_by` on
changes (there is no login).

## How it is put together

- `src/api/client.ts`: the only code that talks to the backend. Every failure becomes an
  `ApiError` with a message that is safe to show, and field-level problems kept by field name.
- `src/lib/view.ts`: directory search/filter/sort state lives in the URL under the API's own
  parameter names. A link reproduces a view, and the export button asks the server for
  exactly what is on screen.
- `src/lib/useAsync.ts`: data loading. `loading` is true from the render in which the inputs
  change, so results for old inputs are never shown as current.
- `src/lib/format.ts`: the one place money, percentages and dates are formatted (currency
  shown as its ISO code, since `$` is five currencies).
- `src/components/charts.tsx`: one-hue bars and a histogram, each with a table view and
  a tooltip. The chart colour was checked with the dataviz palette validator in both themes.
- Money arrives as decimal strings and is parsed only to display it.

## Quality checks that were run

- `npm test`: unit tests (formatting, validation, URL state, API errors) and component tests
  for the directory, import wizard, reference forms and the pay-change, currency-change and
  terminate dialogs. Removing a guard on purpose makes the matching test fail.
- A real browser (Chromium via Playwright) drove the app against the real API on the 10,000
  employee seed, in light and dark: search, filter, sort, paging, export, create, duplicate
  email, future-dated and corrected pay changes, relocation, currency change, terminate,
  import with a bad file then a good one, reference forms, analytics filters and the
  reporting currency, keyboard focus in dialogs. 19 flows, all passing.
- axe-core accessibility audit of all seven screens in both themes: no violations.
