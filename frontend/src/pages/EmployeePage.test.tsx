import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { mockApi, renderApp } from "../test/utils";
import EmployeePage from "./EmployeePage";

const page = () => (
  <Routes>
    <Route path="/employees/:id" element={<EmployeePage />} />
  </Routes>
);

describe("employee page errors", () => {
  it("an employee that doesn't exist is 'not found', with a way back and no pointless retry", async () => {
    mockApi({ "GET /employees/999": { status: 404, body: { detail: "employee not found" } } });
    renderApp(page(), "/employees/999");
    expect(await screen.findByText("Not found")).toBeInTheDocument();
    expect(screen.getByText(/employee not found/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to all employees" })).toHaveAttribute("href", "/");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("a hand-typed address that isn't a number never reaches the server", async () => {
    const api = mockApi();
    renderApp(page(), "/employees/abc");
    expect(await screen.findByText("Not found")).toBeInTheDocument();
    expect(screen.getByText(/no employee with that number/)).toBeInTheDocument();
    expect(api.calls.filter((c) => c.path.startsWith("/employees"))).toHaveLength(0);
  });

  it("a server failure offers a retry, in plain words", async () => {
    mockApi({ "GET /employees/5": { status: 500, body: { detail: "Something went wrong on the server. Nothing you entered was lost: try again." } } });
    renderApp(page(), "/employees/5");
    expect(await screen.findByText("Can't load this right now")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
