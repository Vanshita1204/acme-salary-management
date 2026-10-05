import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, renderApp } from "../test/utils";
import ImportPage from "./ImportPage";

const file = () => new File(["first_name\nAda"], "people.csv", { type: "text/csv" });
const previewRow = { row: 2, first_name: "Ada", last_name: "Lovelace", email: "ada@example.com", company_id: 1, department_id: 1, job_title_id: 1, job_level_id: 3, country: "IN", hire_date: "2024-01-15", currency: "INR", base_pay_amount: "150000.00" };

describe("import wizard", () => {
  it("lists each problem with its row and column and offers no way to import", async () => {
    mockApi({ "POST /import/validate": { body: { ok: false, row_count: 1, errors: [
      { row: 3, column: "job_title", reason: "unknown job title" },
      { row: 5, column: "base_pay_amount", reason: "base pay amount must be greater than zero" },
    ], preview: [previewRow] } } });
    renderApp(<ImportPage />);
    await userEvent.upload(await screen.findByLabelText("CSV file"), file());
    await userEvent.click(screen.getByRole("button", { name: "Check file" }));

    expect(await screen.findByText(/2 problems found: nothing will be imported/)).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "unknown job title" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "job_title" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Import/ })).not.toBeInTheDocument();
  });

  it("needs a file before it checks anything", async () => {
    const api = mockApi();
    renderApp(<ImportPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Check file" }));
    expect(screen.getByText("Choose a CSV file first.")).toBeInTheDocument();
    expect(api.sent("POST", "/import/validate")).toHaveLength(0);
  });

  it("a valid file previews first, and saves only on confirm, with who did it", async () => {
    const api = mockApi({
      "POST /import/validate": { body: { ok: true, row_count: 1, errors: [], preview: [previewRow] } },
      "POST /import/confirm": { status: 201, body: { created: 1, employee_ids: [7] } },
    });
    renderApp(<ImportPage />);
    await userEvent.upload(await screen.findByLabelText("CSV file"), file());
    await userEvent.click(screen.getByRole("button", { name: "Check file" }));
    expect(await screen.findByText("1 rows are valid")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Globex" })).toBeInTheDocument(); // ids shown as names
    expect(api.sent("POST", "/import/confirm")).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: "Import 1 employees" }));
    expect(screen.getByText("Enter who is importing this file.")).toBeInTheDocument();
    expect(api.sent("POST", "/import/confirm")).toHaveLength(0);

    await userEvent.type(screen.getByLabelText(/Imported by/), "hr@acme");
    await userEvent.click(screen.getByRole("button", { name: "Import 1 employees" }));
    expect(await screen.findByText("1 employees imported")).toBeInTheDocument();
    expect(api.sent("POST", "/import/confirm")[0].form?.get("changed_by")).toBe("hr@acme");
  });

  it("if the data changed since the preview, the server's fresh report replaces it and nothing is claimed saved", async () => {
    mockApi({
      "POST /import/validate": { body: { ok: true, row_count: 1, errors: [], preview: [previewRow] } },
      "POST /import/confirm": { status: 422, body: { ok: false, row_count: 0, errors: [{ row: 2, column: "email", reason: "an employee with this email already exists" }], preview: [] } },
    });
    renderApp(<ImportPage />);
    await userEvent.upload(await screen.findByLabelText("CSV file"), file());
    await userEvent.click(screen.getByRole("button", { name: "Check file" }));
    await userEvent.type(await screen.findByLabelText(/Imported by/), "hr");
    await userEvent.click(screen.getByRole("button", { name: "Import 1 employees" }));

    expect(await screen.findByText(/no longer passes validation, so nothing was imported/)).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "an employee with this email already exists" })).toBeInTheDocument();
    expect(screen.queryByText(/employees imported/)).not.toBeInTheDocument();
  });
});
