import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Profile } from "../api/types";
import { mockApi, renderApp } from "../test/utils";
import { ChangeCurrencyModal, CompensationChangeModal, TerminateModal } from "./EmployeeActions";

const item = (id: number, name: string, months: number, amount: string, base = false) => ({
  compensation_type_id: id, name, category: "x", subtype: null, period_months: months, is_base_pay: base, counts_toward_total: true,
  record_id: id, effective_date: "2025-04-01", amount, annual_amount: amount, annual_amount_reporting: null,
});
const profile: Profile = {
  employee: {
    id: 5, code: "EMP-000005", company_id: 1, first_name: "Ravi", last_name: "Kumar", email: "r@x.example",
    department_id: 1, department: "Engineering", job_title_id: 1, job_title: "Software Engineer", job_level_id: 3, job_level: "L3",
    current_country: "IN", currency: "INR", status: "active", hire_date: "2024-01-15", termination_date: null,
  },
  reporting_currency: "USD", rates_as_of: {}, total_compensation: "0", total_compensation_reporting: null,
  current: [item(1, "Base Pay", 1, "220000.00", true), item(2, "Annual Bonus", 12, "250000.00")],
  history: [],
};
const props = { profile, onClose: vi.fn(), onDone: vi.fn() };

describe("change currency (FR-7)", () => {
  it("won't send until every component has an amount, and says which one is missing", async () => {
    const api = mockApi();
    renderApp(<ChangeCurrencyModal {...props} />);
    const dialog = await screen.findByRole("dialog");
    await userEvent.selectOptions(within(dialog).getByLabelText(/New pay currency/), "EUR");
    const [base, bonus] = within(dialog).getAllByLabelText(/^New amount/);
    await userEvent.type(base, "2600");
    await userEvent.click(within(dialog).getByRole("button", { name: "Change currency" }));

    expect(within(dialog).getAllByText("Enter an amount.")).toHaveLength(1);
    expect(bonus).toHaveAttribute("aria-invalid", "true");
    expect(api.sent("POST", "/employees/5/change-currency")).toHaveLength(0);
  });

  it("sends one amount per component, typed by a person, with the reason and date", async () => {
    const api = mockApi({ "POST /employees/5/change-currency": { body: { employee: {}, records: [] } } });
    const onDone = vi.fn();
    renderApp(<ChangeCurrencyModal {...props} onDone={onDone} />);
    const dialog = await screen.findByRole("dialog");
    await userEvent.selectOptions(within(dialog).getByLabelText(/New pay currency/), "EUR");
    const [base, bonus] = within(dialog).getAllByLabelText(/^New amount/);
    await userEvent.type(base, "2,600");
    await userEvent.type(bonus, "3000");
    await userEvent.type(within(dialog).getByLabelText(/Changed by/), "hr@acme");
    await userEvent.click(within(dialog).getByRole("button", { name: "Change currency" }));

    expect(onDone).toHaveBeenCalledWith(expect.stringContaining("Now paid in EUR"));
    expect(api.sent("POST", "/employees/5/change-currency")[0].json).toMatchObject({
      currency: "EUR",
      amounts: [{ compensation_type_id: 1, amount: "2600" }, { compensation_type_id: 2, amount: "3000" }],
      change_reason_id: 2, // "Market adjustment" is the default reason
      changed_by: "hr@acme",
    });
  });

  it("fills in suggestions from the stored rate, which stay editable", async () => {
    mockApi({ "GET /exchange-rates/convert": ({ query }) => ({ body: { converted: String(Number(query.get("amount")) / 100) } }) });
    renderApp(<ChangeCurrencyModal {...props} />);
    const dialog = await screen.findByRole("dialog");
    await userEvent.selectOptions(within(dialog).getByLabelText(/New pay currency/), "EUR");
    await userEvent.click(within(dialog).getByRole("button", { name: "Suggest from current rates" }));
    expect(await within(dialog).findByText(/suggestions, not conversions/)).toBeInTheDocument();
    const [base] = within(dialog).getAllByLabelText(/^New amount/);
    expect(base).toHaveValue("2200");
    await userEvent.clear(base);
    await userEvent.type(base, "2100");
    expect(base).toHaveValue("2100");
  });

  it("shows the server's refusal and keeps the typed amounts", async () => {
    mockApi({ "POST /employees/5/change-currency": { status: 409, body: { detail: "employee has future-dated compensation changes" } } });
    renderApp(<ChangeCurrencyModal {...props} />);
    const dialog = await screen.findByRole("dialog");
    await userEvent.selectOptions(within(dialog).getByLabelText(/New pay currency/), "EUR");
    const [base, bonus] = within(dialog).getAllByLabelText(/^New amount/);
    await userEvent.type(base, "2600");
    await userEvent.type(bonus, "3000");
    await userEvent.type(within(dialog).getByLabelText(/Changed by/), "hr");
    await userEvent.click(within(dialog).getByRole("button", { name: "Change currency" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("future-dated compensation changes");
    expect(base).toHaveValue("2600");
  });
});

describe("record a pay change (FR-4)", () => {
  it("allows a future date and says when it takes effect", async () => {
    const future = new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10);
    mockApi();
    renderApp(<CompensationChangeModal {...props} />);
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/Effective date/), { target: { value: future } }); // date inputs: set, don't type
    expect(within(dialog).getByText(/becomes current on that date/)).toBeInTheDocument();
  });

  it("refuses zero for base pay but allows it for other components", async () => {
    const api = mockApi({ "POST /employees/5/compensation": { status: 201, body: { id: 1, effective_date: "2026-10-05", is_current: true } } });
    renderApp(<CompensationChangeModal {...props} />);
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/^New amount/), "0");
    await userEvent.type(within(dialog).getByLabelText(/Changed by/), "hr");
    await userEvent.click(within(dialog).getByRole("button", { name: "Record change" }));
    expect(within(dialog).getByText("Must be greater than zero.")).toBeInTheDocument();
    expect(api.sent("POST", "/employees/5/compensation")).toHaveLength(0);

    await userEvent.selectOptions(within(dialog).getByLabelText(/Compensation type/), "Annual Bonus");
    await userEvent.click(within(dialog).getByRole("button", { name: "Record change" }));
    expect(api.sent("POST", "/employees/5/compensation")).toHaveLength(1);
  });
});

describe("terminate", () => {
  it("explains what happens and does nothing until confirmed", async () => {
    const api = mockApi({ "POST /employees/5/terminate": { body: {} } });
    const onDone = vi.fn();
    renderApp(<TerminateModal {...props} onDone={onDone} />);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/Nothing is deleted/);
    expect(api.sent("POST", "/employees/5/terminate")).toHaveLength(0);
    await userEvent.click(within(dialog).getByRole("button", { name: "Terminate" }));
    expect(onDone).toHaveBeenCalledWith(expect.stringContaining("terminated"));
    expect(api.sent("POST", "/employees/5/terminate")).toHaveLength(1);
  });
});
