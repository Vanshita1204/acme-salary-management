// The directory's search/filter/sort state. It lives in the URL under the same names the
// API uses, so a link reproduces the view and the export button always asks the server
// for exactly what is on screen (FR-6).

import type { Params } from "../api/client";
import type { Status } from "../api/types";

export type SortKey = "name" | "hire_date" | "level" | "compensation";
export type Order = "asc" | "desc";

export interface View {
  q: string;
  departmentIds: number[];
  countries: string[];
  titleIds: number[];
  levelIds: number[];
  statuses: Status[];
  sort: SortKey;
  order: Order;
}

export const EMPTY_VIEW: View = {
  q: "", departmentIds: [], countries: [], titleIds: [], levelIds: [], statuses: [], sort: "name", order: "asc",
};

const SORTS: SortKey[] = ["name", "hire_date", "level", "compensation"];
const STATUSES: Status[] = ["active", "on_leave", "terminated"];

const numbers = (values: string[]) => values.map(Number).filter((n) => Number.isInteger(n) && n > 0);

export function parseView(search: URLSearchParams): View {
  const sort = search.get("sort") as SortKey | null;
  const order = search.get("order");
  return {
    q: search.get("q") ?? "",
    departmentIds: numbers(search.getAll("department_id")),
    countries: search.getAll("country").map((c) => c.toUpperCase()),
    titleIds: numbers(search.getAll("job_title_id")),
    levelIds: numbers(search.getAll("job_level_id")),
    statuses: search.getAll("status").filter((s): s is Status => STATUSES.includes(s as Status)),
    sort: sort && SORTS.includes(sort) ? sort : "name",
    order: order === "desc" ? "desc" : "asc",
  };
}

/** The API's names for the view's filters (no sort/paging). */
export function filterParams(view: View): Params {
  return {
    q: view.q.trim() || undefined,
    department_id: view.departmentIds,
    country: view.countries,
    job_title_id: view.titleIds,
    job_level_id: view.levelIds,
  };
}

export function viewParams(view: View): Params {
  return { ...filterParams(view), status: view.statuses, sort: view.sort, order: view.order };
}

/** URL form: defaults are omitted to keep links short. */
export function viewToSearch(view: View, cursor?: string | null): URLSearchParams {
  const params = viewParams(view);
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item === undefined || item === null || item === "") continue;
      search.append(key, String(item));
    }
  }
  if (view.sort === "name") search.delete("sort");
  if (view.order === "asc") search.delete("order");
  if (cursor) search.set("cursor", cursor);
  return search;
}

export function hasFilters(view: View): boolean {
  return Boolean(
    view.q.trim() || view.departmentIds.length || view.countries.length || view.titleIds.length || view.levelIds.length || view.statuses.length,
  );
}

/** Clicking a column: same column flips the order, a new one starts ascending (compensation: highest first). */
export function nextSort(view: View, column: SortKey): Pick<View, "sort" | "order"> {
  if (view.sort === column) return { sort: column, order: view.order === "asc" ? "desc" : "asc" };
  return { sort: column, order: column === "compensation" ? "desc" : "asc" };
}
