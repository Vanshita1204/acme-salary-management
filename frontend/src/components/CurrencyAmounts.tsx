import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { CurrentItem } from "../api/types";
import { formatMoney, perPeriod } from "../lib/format";
import { Field } from "./forms";

/**
 * One amount per current compensation type, in the new currency (FR-7). Amounts are
 * always typed by a person: nothing is converted automatically. "Suggest" fills in
 * today's rate as a starting point they can change.
 */
export function CurrencyAmounts({ items, from, to, values, onChange, errors }: {
  items: CurrentItem[];
  from: string;
  to: string;
  values: Record<number, string>;
  onChange: (typeId: number, value: string) => void;
  errors: Record<string, string>;
}) {
  const [suggesting, setSuggesting] = useState(false);
  const [note, setNote] = useState<string | undefined>();

  async function suggest() {
    setSuggesting(true);
    setNote(undefined);
    try {
      for (const item of items) {
        const result = await api.get<{ converted: string }>("/exchange-rates/convert", { amount: item.amount, from, to });
        onChange(item.compensation_type_id, result.converted);
      }
      setNote("Filled in at today's stored rate. Check each amount: these are suggestions, not conversions.");
    } catch (caught) {
      setNote(caught instanceof ApiError ? caught.message : "Couldn't look up a rate.");
    } finally {
      setSuggesting(false);
    }
  }

  return (
    <fieldset className="card" style={{ marginBottom: ".85rem" }}>
      <legend>Amounts in {to || "the new currency"}</legend>
      <p className="muted" style={{ marginTop: 0 }}>
        Enter what each pay component will be in {to || "the new currency"}. Every current component needs an amount, and they are all saved together.
      </p>
      {items.map((item) => (
        <div className="amount-row" key={item.compensation_type_id}>
          <div>
            <strong>{item.name}</strong>
            <div className="was">Now {formatMoney(item.amount, from)} {perPeriod(item.period_months)}</div>
          </div>
          <Field label={`New amount ${perPeriod(item.period_months)}`} error={errors[`amount-${item.compensation_type_id}`]}>
            {(props) => (
              <input
                {...props}
                inputMode="decimal"
                value={values[item.compensation_type_id] ?? ""}
                onChange={(event) => onChange(item.compensation_type_id, event.target.value)}
              />
            )}
          </Field>
          <span />
        </div>
      ))}
      <div className="actions">
        <button type="button" onClick={suggest} disabled={suggesting || !to}>
          {suggesting ? "Looking up rates…" : "Suggest from current rates"}
        </button>
        {note && <span className="muted">{note}</span>}
      </div>
    </fieldset>
  );
}
