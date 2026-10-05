import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, renderApp } from "../test/utils";
import DirectoryPage from "./DirectoryPage";

const person = (id: number, last: string) => ({
  id, code: `EMP-00000${id}`, first_name: "Ada", last_name: last, email: `${last}@x.example`, department: "Engineering",
  job_title: "Software Engineer", job_level: "L3", current_country: "IN", status: "active", hire_date: "2024-01-15",
  currency: "INR", total_compensation: "2400000.00", total_compensation_reporting: "28800.00",
});
const page = (items: unknown[], extra = {}) => ({ body: { items, next_cursor: null, prev_cursor: null, reporting_currency: "USD", rates_as_of: { INR: "2026-10-05", USD: "2026-10-05" }, ...extra } });

describe("directory", () => {
  it("shows pay in local and reporting currency, with the date of the rates", async () => {
    mockApi({ "GET /employees": page([person(1, "Lovelace")]) });
    renderApp(<DirectoryPage />);
    expect(await screen.findByRole("link", { name: "Ada Lovelace" })).toHaveAttribute("href", "/employees/1");
    expect(screen.getByRole("cell", { name: "INR 2,400,000.00" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "28,800.00" })).toBeInTheDocument();
    expect(screen.getByText("Exchange rates as of 5 Oct 2026.")).toBeInTheDocument();
  });

  it("asks the server for exactly the filters in the URL, and the export link carries the same ones", async () => {
    const api = mockApi({ "GET /employees": page([person(1, "Kumar")]) });
    renderApp(<DirectoryPage />, "/?q=kumar&country=IN&country=DE&department_id=2&sort=hire_date&order=desc");
    await screen.findByRole("link", { name: "Ada Kumar" });
    const request = api.sent("GET", "/employees")[0].query;
    expect(request.get("q")).toBe("kumar");
    expect(request.getAll("country")).toEqual(["IN", "DE"]);
    expect(request.getAll("department_id")).toEqual(["2"]);
    expect([request.get("sort"), request.get("order"), request.get("limit")]).toEqual(["hire_date", "desc", "50"]);
    expect(request.get("reporting_currency")).toBe("USD");

    const href = new URL(screen.getByRole("link", { name: "Export CSV" }).getAttribute("href")!, "http://test");
    expect(href.pathname).toBe("/api/employees/export");
    for (const key of ["q", "country", "department_id", "sort", "order", "reporting_currency"]) {
      expect(href.searchParams.getAll(key)).toEqual(request.getAll(key));
    }
    expect(href.searchParams.has("cursor")).toBe(false);
    expect(href.searchParams.has("limit")).toBe(false);
  });

  it("pages with the server's cursors", async () => {
    const api = mockApi({
      "GET /employees": ({ query }) => (query.get("cursor") === "NEXT" ? page([person(2, "Second")], { prev_cursor: "PREV" }) : page([person(1, "First")], { next_cursor: "NEXT" })),
    });
    renderApp(<DirectoryPage />);
    await screen.findByRole("link", { name: "Ada First" });
    expect(screen.getByRole("button", { name: /Previous/ })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByRole("link", { name: "Ada Second" })).toBeInTheDocument();
    expect(api.sent("GET", "/employees").at(-1)!.query.get("cursor")).toBe("NEXT");
    expect(screen.getByRole("button", { name: /Next/ })).toBeDisabled();
  });

  it("says so when nothing matches, and when the server can't be reached", async () => {
    mockApi({ "GET /employees": page([]) });
    const view = renderApp(<DirectoryPage />, "/?q=zzz");
    expect(await screen.findByText("No employees match these filters.")).toBeInTheDocument();
    view.unmount();

    mockApi({ "GET /employees": { status: 500, body: { detail: "boom" } } });
    renderApp(<DirectoryPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
