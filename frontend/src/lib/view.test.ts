import { describe, expect, it } from "vitest";
import { EMPTY_VIEW, filterParams, hasFilters, nextSort, parseView, viewParams, viewToSearch } from "./view";

describe("directory view in the URL", () => {
  it("round-trips through the API's own parameter names", () => {
    const view = { ...EMPTY_VIEW, q: "kumar", departmentIds: [2, 5], countries: ["IN", "US"], statuses: ["active" as const], sort: "compensation" as const, order: "desc" as const };
    const search = viewToSearch(view);
    expect(search.toString()).toBe("q=kumar&department_id=2&department_id=5&country=IN&country=US&status=active&sort=compensation&order=desc");
    expect(parseView(search)).toEqual(view);
  });
  it("leaves defaults out of the URL", () => {
    expect(viewToSearch(EMPTY_VIEW).toString()).toBe("");
  });
  it("carries a page cursor only when given", () => {
    expect(viewToSearch(EMPTY_VIEW, "abc").toString()).toBe("cursor=abc");
  });
  it("ignores values it doesn't understand", () => {
    const view = parseView(new URLSearchParams("sort=salary&order=sideways&status=gone&department_id=x&department_id=-3&department_id=7&country=in"));
    expect(view).toMatchObject({ sort: "name", order: "asc", statuses: [], departmentIds: [7], countries: ["IN"] });
  });
  it("the export asks for exactly the on-screen view", () => {
    const view = parseView(new URLSearchParams("q=ab&job_level_id=3&sort=hire_date&order=desc"));
    expect(viewParams(view)).toMatchObject({ q: "ab", job_level_id: [3], sort: "hire_date", order: "desc" });
  });
  it("analytics reuse the filters but never the sort", () => {
    expect(Object.keys(filterParams(EMPTY_VIEW)).sort()).toEqual(["country", "department_id", "job_level_id", "job_title_id", "q"]);
  });
  it("knows when a filter is active", () => {
    expect(hasFilters(EMPTY_VIEW)).toBe(false);
    expect(hasFilters({ ...EMPTY_VIEW, q: "  " })).toBe(false);
    expect(hasFilters({ ...EMPTY_VIEW, levelIds: [1] })).toBe(true);
  });
  it("clicking a column flips its order; a new column starts sensibly", () => {
    expect(nextSort(EMPTY_VIEW, "name")).toEqual({ sort: "name", order: "desc" });
    expect(nextSort(EMPTY_VIEW, "hire_date")).toEqual({ sort: "hire_date", order: "asc" });
    expect(nextSort(EMPTY_VIEW, "compensation")).toEqual({ sort: "compensation", order: "desc" });
  });
});
