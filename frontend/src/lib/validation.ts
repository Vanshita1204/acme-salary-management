// Quick checks so obvious mistakes are caught before a round trip. The server still
// enforces every rule (the database constraints are the real guard, §7).

export interface Parsed { value?: string; error?: string }

/** A money amount typed by a person: digits with at most 2 decimals; thousands commas allowed. */
export function parseAmount(text: string, options: { positive?: boolean } = {}): Parsed {
  const cleaned = text.trim().replace(/,/g, "");
  if (cleaned === "") return { error: "Enter an amount." };
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) {
    return { error: /^-/.test(cleaned) ? "Amounts can't be negative." : "Use digits with at most 2 decimal places." };
  }
  if (cleaned.length > 15) return { error: "That amount is too large." };
  if (options.positive && Number(cleaned) <= 0) return { error: "Must be greater than zero." };
  return { value: cleaned };
}

export function requireText(text: string, what: string): string | undefined {
  return text.trim() ? undefined : `Enter ${what}.`;
}

export function validEmail(text: string): string | undefined {
  const value = text.trim();
  if (!value) return "Enter an email address.";
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) ? undefined : "Enter a valid email address.";
}

export function requireDate(text: string, what = "a date"): string | undefined {
  return /^\d{4}-\d{2}-\d{2}$/.test(text) ? undefined : `Choose ${what}.`;
}

/** Collects errors by field name; `ok` is true when there are none. */
export function collect(checks: Record<string, string | undefined>): { errors: Record<string, string>; ok: boolean } {
  const errors = Object.fromEntries(Object.entries(checks).filter((entry): entry is [string, string] => Boolean(entry[1])));
  return { errors, ok: Object.keys(errors).length === 0 };
}
