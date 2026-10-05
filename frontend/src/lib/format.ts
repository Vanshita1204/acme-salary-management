// One place for how money, percentages and dates look, so every screen agrees (§7).

const NO_VALUE = "—";

function parse(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

const formatters = new Map<string, Intl.NumberFormat>();
function currencyFormat(currency: string): Intl.NumberFormat {
  const key = currency;
  let format = formatters.get(key);
  if (!format) {
    try {
      format = new Intl.NumberFormat("en-US", {
        style: "currency",
        currency,
        currencyDisplay: "code",
      });
    } catch {
      format = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    formatters.set(key, format);
  }
  return format;
}

/** "INR 2,400,000.00": the ISO code, not a symbol, since `$` is five currencies. */
export function formatMoney(value: string | number | null | undefined, currency: string): string {
  const number = parse(value);
  if (number === null) return NO_VALUE;
  return currencyFormat(currency).format(number).replace(/ /g, " ");
}

/** The amount alone, with the currency's own decimals, for columns headed by currency. */
export function formatAmount(value: string | number | null | undefined, currency: string): string {
  const number = parse(value);
  if (number === null) return NO_VALUE;
  const parts = currencyFormat(currency).formatToParts(number);
  return parts
    .filter((part) => part.type !== "currency" && part.type !== "literal")
    .map((part) => part.value)
    .join("");
}

/** "+9.09%" / "-2.66%"; the backend sends percent values already multiplied by 100. */
export function formatPercent(value: string | number | null | undefined, signed = true): string {
  const number = parse(value);
  if (number === null) return NO_VALUE;
  const text = Math.abs(number).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (number > 0) return `${signed ? "+" : ""}${text}%`;
  if (number < 0) return `-${text}%`;
  return `${text}%`;
}

/** A 0–1 share as "34.1%". */
export function formatShare(value: string | number | null | undefined): string {
  const number = parse(value);
  if (number === null) return NO_VALUE;
  return `${(number * 100).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
}

export function formatInteger(value: number | null | undefined): string {
  return value === null || value === undefined ? NO_VALUE : value.toLocaleString("en-US");
}

const dateFormat = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

/** "5 Oct 2026" from "2026-10-05" (read as a calendar date, never shifted by time zone). */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return NO_VALUE;
  const date = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  return Number.isNaN(date.getTime()) ? iso : dateFormat.format(date);
}

export function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

const PERIODS: Record<number, string> = { 1: "Monthly", 3: "Quarterly", 6: "Semi-annual", 12: "Annual" };
export function periodLabel(months: number): string {
  return PERIODS[months] ?? `Every ${months} months`;
}

/** "per month", "per year"... for an input whose amount covers `months`. */
export function perPeriod(months: number): string {
  if (months === 1) return "per month";
  if (months === 12) return "per year";
  return `per ${months} months`;
}

export const STATUS_LABELS = { active: "Active", on_leave: "On leave", terminated: "Terminated" } as const;

export function fullName(person: { first_name: string; last_name: string }): string {
  return `${person.first_name} ${person.last_name}`;
}

/** A sentence about the exchange rates behind a figure (FR-8: every view shows their date). */
export function describeRates(ratesAsOf: Record<string, string>): string {
  const dates = [...new Set(Object.values(ratesAsOf))].sort();
  if (dates.length === 0) return "No exchange rates were needed.";
  if (dates.length === 1) return `Exchange rates as of ${formatDate(dates[0])}.`;
  return `Exchange rates from ${formatDate(dates[0])} to ${formatDate(dates[dates.length - 1])}.`;
}

/** "USD 44K": for chart axes where the exact amount is in the tooltip and table. */
export function formatCompact(value: string | number | null | undefined, currency: string): string {
  const number = parse(value);
  if (number === null) return NO_VALUE;
  const text = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(number);
  return `${currency} ${text}`;
}
