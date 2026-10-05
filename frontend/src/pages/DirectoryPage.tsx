import { useCallback, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { API_URL, api, queryString } from "../api/client";
import type { DirectoryPage as Page } from "../api/types";
import { FilterBar } from "../components/FilterBar";
import { AsyncView, Badge, EmptyState, PageHeader, RatesNote } from "../components/ui";
import { formatAmount, formatDate, fullName, STATUS_LABELS } from "../lib/format";
import { useReporting } from "../lib/context";
import { useAsync } from "../lib/useAsync";
import { nextSort, parseView, viewParams, viewToSearch, type SortKey, type View } from "../lib/view";

const PAGE_SIZE = 50;

const STATUS_TONE = { active: "good", on_leave: "warning", terminated: "neutral" } as const;

export default function DirectoryPage() {
  const [search, setSearch] = useSearchParams();
  const { currency } = useReporting();
  const view = useMemo(() => parseView(search), [search]);
  const cursor = search.get("cursor");

  // Any change to the view starts again from the first page.
  const change = useCallback(
    (changes: Partial<View>) => setSearch(viewToSearch({ ...parseView(search), ...changes })),
    [search, setSearch],
  );

  const state = useAsync(
    () => api.get<Page>("/employees", { ...viewParams(view), cursor, limit: PAGE_SIZE, reporting_currency: currency }),
    [search.toString(), currency],
  );
  const exportHref = `${API_URL}/employees/export${queryString({ ...viewParams(view), reporting_currency: currency })}`;

  return (
    <>
      <PageHeader
        title="Employees"
        subtitle="Search and filter the directory; open a person to see their pay and history."
        actions={
          <>
            <a className="button" href={exportHref} download>Export CSV</a>
            <Link className="button primary" to="/employees/new">Add employee</Link>
          </>
        }
      />
      <div className="card">
        <FilterBar view={view} onChange={change} showStatus />
        <AsyncView state={state}>
          {(page) => (
            <>
              {page.items.length === 0 ? (
                <EmptyState>No employees match these filters.</EmptyState>
              ) : (
                <div className="table-wrap" tabIndex={0}>
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">Code</th>
                        <SortHeader column="name" view={view} onSort={change}>Name</SortHeader>
                        <th scope="col">Department</th>
                        <th scope="col">Title</th>
                        <SortHeader column="level" view={view} onSort={change}>Level</SortHeader>
                        <th scope="col">Country</th>
                        <th scope="col">Status</th>
                        <SortHeader column="hire_date" view={view} onSort={change}>Hired</SortHeader>
                        <th scope="col" className="num">Total pay (local)</th>
                        <SortHeader column="compensation" view={view} onSort={change} numeric>
                          Total pay ({page.reporting_currency})
                        </SortHeader>
                      </tr>
                    </thead>
                    <tbody>
                      {page.items.map((person) => (
                        <tr key={person.id}>
                          <td className="nowrap">{person.code}</td>
                          <td className="nowrap"><Link to={`/employees/${person.id}`}>{fullName(person)}</Link></td>
                          <td>{person.department}</td>
                          <td>{person.job_title}</td>
                          <td>{person.job_level}</td>
                          <td>{person.current_country}</td>
                          <td><Badge tone={STATUS_TONE[person.status]}>{STATUS_LABELS[person.status]}</Badge></td>
                          <td>{formatDate(person.hire_date)}</td>
                          <td className="num">
                            {person.total_compensation === null ? "—" : `${person.currency} ${formatAmount(person.total_compensation, person.currency)}`}
                          </td>
                          <td className="num">{formatAmount(person.total_compensation_reporting, page.reporting_currency)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="pager">
                <button type="button" disabled={!page.prev_cursor} onClick={() => setSearch(viewToSearch(view, page.prev_cursor))}>
                  ← Previous
                </button>
                <span className="muted">
                  Showing {page.items.length} {page.items.length === 1 ? "employee" : "employees"}
                  {page.next_cursor ? " (more on the next page)" : ""}
                </span>
                <button type="button" disabled={!page.next_cursor} onClick={() => setSearch(viewToSearch(view, page.next_cursor))}>
                  Next →
                </button>
              </div>
              <RatesNote ratesAsOf={page.rates_as_of} />
            </>
          )}
        </AsyncView>
      </div>
    </>
  );
}

function SortHeader({ column, view, onSort, children, numeric }: {
  column: SortKey;
  view: View;
  onSort: (changes: Partial<View>) => void;
  children: React.ReactNode;
  numeric?: boolean;
}) {
  const active = view.sort === column;
  const sortLabel = active ? (view.order === "asc" ? "ascending" : "descending") : "none";
  return (
    <th scope="col" className={numeric ? "num" : undefined} aria-sort={active ? (view.order === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" onClick={() => onSort(nextSort(view, column))} title={`Sort by ${column.replace("_", " ")} (currently ${sortLabel})`}>
        {children}
        {active ? (view.order === "asc" ? " ▲" : " ▼") : ""}
      </button>
    </th>
  );
}
