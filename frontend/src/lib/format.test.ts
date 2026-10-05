import { describe, expect, it } from "vitest";
import { describeRates, formatAmount, formatCompact, formatDate, formatMoney, formatPercent, formatShare, periodLabel, perPeriod } from "./format";

describe("money", () => {
  it("shows the ISO code, not a symbol, since $ is five currencies", () => {
    expect(formatMoney("2400000", "INR")).toBe("INR 2,400,000.00");
    expect(formatMoney("1234.5", "USD")).toBe("USD 1,234.50");
  });
  it("uses each currency's own number of decimals", () => {
    expect(formatMoney("12345", "JPY")).toBe("JPY 12,345");
    expect(formatAmount("1234.5", "USD")).toBe("1,234.50");
    expect(formatAmount("12345", "JPY")).toBe("12,345");
  });
  it("shows a dash instead of a number when there is no value", () => {
    expect(formatMoney(null, "USD")).toBe("—");
    expect(formatMoney(undefined, "USD")).toBe("—");
    expect(formatAmount("", "USD")).toBe("—");
  });
  it("falls back to plain decimals for a code Intl doesn't know", () => {
    expect(formatMoney("5", "ZZZ")).toMatch(/5\.00/);
  });
  it("abbreviates for chart axes", () => {
    expect(formatCompact("791532043.8", "USD")).toBe("USD 791.5M");
    expect(formatCompact("44100", "EUR")).toBe("EUR 44.1K");
  });
});

describe("percent and share", () => {
  it("signs changes and keeps two decimals", () => {
    expect(formatPercent("9.09")).toBe("+9.09%");
    expect(formatPercent("-2.66")).toBe("-2.66%");
    expect(formatPercent("0")).toBe("0.00%");
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent("20", false)).toBe("20.00%");
  });
  it("turns a 0–1 share into a percentage", () => {
    expect(formatShare("0.3414")).toBe("34.1%");
  });
});

describe("dates", () => {
  it("reads a calendar date without shifting it by time zone", () => {
    expect(formatDate("2026-10-05")).toBe("5 Oct 2026");
    expect(formatDate("2026-01-01")).toBe("1 Jan 2026");
    expect(formatDate("2026-12-31T23:59:59Z")).toBe("31 Dec 2026");
  });
  it("shows a dash for nothing", () => {
    expect(formatDate(null)).toBe("—");
  });
});

describe("periods", () => {
  it("names the usual ones and spells out the rest", () => {
    expect(periodLabel(1)).toBe("Monthly");
    expect(periodLabel(3)).toBe("Quarterly");
    expect(periodLabel(12)).toBe("Annual");
    expect(periodLabel(2)).toBe("Every 2 months");
    expect(perPeriod(1)).toBe("per month");
    expect(perPeriod(12)).toBe("per year");
    expect(perPeriod(3)).toBe("per 3 months");
  });
});

describe("describeRates (every figure says which rates it used)", () => {
  it("names one date, a range, or that none were needed", () => {
    expect(describeRates({ EUR: "2026-10-05", INR: "2026-10-05" })).toBe("Exchange rates as of 5 Oct 2026.");
    expect(describeRates({ EUR: "2026-10-03", INR: "2026-10-05" })).toBe("Exchange rates from 3 Oct 2026 to 5 Oct 2026.");
    expect(describeRates({})).toBe("No exchange rates were needed.");
  });
});
