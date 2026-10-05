import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, renderApp } from "../test/utils";
import ReferencePage from "./ReferencePage";

describe("compensation types", () => {
  it("shows the existing types, and a duplicate is explained inline without losing what was typed", async () => {
    const api = mockApi({ "POST /compensation-types": { status: 409, body: { detail: "compensation type with this category and subtype already exists" } } });
    renderApp(<ReferencePage />);
    const list = await screen.findByRole("heading", { name: /Compensation types \(2\)/ });
    expect(list).toBeInTheDocument();

    const form = screen.getByRole("form", { name: "Add compensation type" });
    await userEvent.type(within(form).getByLabelText(/^Name/), "Spot Bonus");
    await userEvent.type(within(form).getByLabelText(/^Category/), "bonus");
    await userEvent.type(within(form).getByLabelText("Subtype"), "annual");
    await userEvent.click(within(form).getByRole("button", { name: "Add type" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent(/already exists/);
    expect(within(form).getByLabelText(/^Name/)).toHaveValue("Spot Bonus");
    expect(api.sent("POST", "/compensation-types")[0].json).toEqual({
      name: "Spot Bonus", category: "bonus", subtype: "annual", period_months: 1, counts_toward_total: true,
    });
  });

  it("asks for what's missing before sending anything", async () => {
    const api = mockApi();
    renderApp(<ReferencePage />);
    const form = await screen.findByRole("form", { name: "Add compensation type" });
    await userEvent.click(within(form).getByRole("button", { name: "Add type" }));
    expect(within(form).getByText("Enter a name.")).toBeInTheDocument();
    expect(within(form).getByText("Enter a category.")).toBeInTheDocument();
    expect(api.sent("POST", "/compensation-types")).toHaveLength(0);
  });

  it("offers a custom number of months and sends it", async () => {
    const api = mockApi({ "POST /compensation-types": { status: 201, body: { id: 9, name: "Retention", category: "bonus", subtype: null, period_months: 24, is_base_pay: false, counts_toward_total: false, created_at: "" } } });
    renderApp(<ReferencePage />);
    const form = await screen.findByRole("form", { name: "Add compensation type" });
    await userEvent.type(within(form).getByLabelText(/^Name/), "Retention");
    await userEvent.type(within(form).getByLabelText(/^Category/), "bonus");
    await userEvent.selectOptions(within(form).getByLabelText(/How often/), "custom");
    await userEvent.type(within(form).getByLabelText(/Months between/), "24");
    await userEvent.click(within(form).getByRole("checkbox"));
    await userEvent.click(within(form).getByRole("button", { name: "Add type" }));
    expect(await screen.findByText(/now available when recording pay changes/)).toBeInTheDocument();
    expect(api.sent("POST", "/compensation-types")[0].json).toMatchObject({ period_months: 24, counts_toward_total: false, subtype: null });
  });
});

describe("change reasons", () => {
  it("fills the code in from the label, which stays editable", async () => {
    mockApi();
    renderApp(<ReferencePage />, "/reference?tab=reasons");
    const form = await screen.findByRole("form", { name: "Add change reason" });
    await userEvent.type(within(form).getByLabelText(/^Label/), "Spot award!");
    expect(within(form).getByLabelText(/^Code/)).toHaveValue("spot_award");
    await userEvent.clear(within(form).getByLabelText(/^Code/));
    await userEvent.type(within(form).getByLabelText(/^Code/), "bonus_spot");
    await userEvent.type(within(form).getByLabelText(/^Label/), " 2");
    expect(within(form).getByLabelText(/^Code/)).toHaveValue("bonus_spot");
  });
});
