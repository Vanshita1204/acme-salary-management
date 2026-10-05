import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { AsyncView, RatesNote } from "./ui";

const wrap = (ui: React.ReactElement) => render(<MemoryRouter>{ui}</MemoryRouter>);

describe("AsyncView", () => {
  it("never lets old figures pass as current when a refresh fails", () => {
    const state = { data: { total: 42 }, error: new ApiError(503, "The database isn't reachable right now."), loading: false, reload: () => {} };
    wrap(<AsyncView state={state}>{(d) => <p>Total {d.total}</p>}</AsyncView>);
    expect(screen.getByRole("alert")).toHaveTextContent("The database isn't reachable right now.");
    expect(screen.getByRole("alert")).toHaveTextContent(/may not match your latest choices/);
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByText("Total 42").parentElement).toHaveClass("reloading"); // shown dimmed
  });

  it("shows old content quietly while new content loads", () => {
    const state = { data: { total: 1 }, error: undefined, loading: true, reload: () => {} };
    wrap(<AsyncView state={state}>{(d) => <p>Total {d.total}</p>}</AsyncView>);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("Total 1").parentElement).toHaveAttribute("aria-busy", "true");
  });

  it("with nothing to show, a failure is the whole view", () => {
    const state = { data: undefined, error: new ApiError(500, "Something went wrong on the server."), loading: false, reload: () => {} };
    wrap(<AsyncView state={state}>{() => <p>never</p>}</AsyncView>);
    expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong on the server.");
    expect(screen.queryByText("never")).not.toBeInTheDocument();
  });
});

describe("RatesNote", () => {
  it("says which rates were used", () => {
    wrap(<RatesNote ratesAsOf={{ EUR: "2026-10-05" }} currency="EUR" />);
    expect(screen.getByText(/All amounts in EUR\. Exchange rates as of 5 Oct 2026\./)).toBeInTheDocument();
  });
  it("says when no rate was needed", () => {
    wrap(<RatesNote ratesAsOf={{}} />);
    expect(screen.getByText("No exchange rates were needed.")).toBeInTheDocument();
  });
  it("doesn't claim no rate was needed while also saying people were left out for lack of one", () => {
    wrap(<RatesNote ratesAsOf={{}} excluded={3} />);
    expect(screen.queryByText(/No exchange rates were needed/)).not.toBeInTheDocument();
    expect(screen.getByText(/3 employees are left out: no exchange rate is stored for their currency/)).toBeInTheDocument();
  });
  it("uses the singular for one person", () => {
    wrap(<RatesNote ratesAsOf={{ USD: "2026-10-05" }} excluded={1} />);
    expect(screen.getByText(/1 employee is left out/)).toBeInTheDocument();
  });
});
