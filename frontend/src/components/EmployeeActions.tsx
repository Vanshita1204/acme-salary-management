import { useState } from "react";
import { api } from "../api/client";
import type { ChangeResult, CompensationChange, Employee, Profile } from "../api/types";
import { useActor, useLoadedReference } from "../lib/context";
import { formatDate, perPeriod, todayIso } from "../lib/format";
import { collect, parseAmount, requireDate, requireText, validEmail } from "../lib/validation";
import { CurrencyAmounts } from "./CurrencyAmounts";
import { ConfirmModal, Field, FormError, Modal, useSubmit } from "./forms";
import { Alert } from "./ui";

interface ActionProps {
  profile: Profile;
  onClose: () => void;
  /** Called after a successful save, with a sentence to show the user. */
  onDone: (message: string) => void;
}

function Footer({ submitting, onClose, label }: { submitting: boolean; onClose: () => void; label: string }) {
  return (
    <div className="modal-foot">
      <button type="button" onClick={onClose} disabled={submitting}>Cancel</button>
      <button type="submit" className="primary" disabled={submitting}>{submitting ? "Saving…" : label}</button>
    </div>
  );
}

// --- edit details (FR-3) ---

export function EditEmployeeModal({ profile, onClose, onDone }: ActionProps) {
  const ref = useLoadedReference();
  const employee = profile.employee;
  const [form, setForm] = useState({
    company_id: String(employee.company_id),
    first_name: employee.first_name,
    last_name: employee.last_name,
    email: employee.email,
    department_id: String(employee.department_id),
    job_title_id: String(employee.job_title_id),
    job_level_id: String(employee.job_level_id),
    status: employee.status as string,
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const { submitting, error, run } = useSubmit();
  const set = (name: keyof typeof form) => (event: { target: { value: string } }) => setForm({ ...form, [name]: event.target.value });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const check = collect({
      first_name: requireText(form.first_name, "a first name"),
      last_name: requireText(form.last_name, "a last name"),
      email: validEmail(form.email),
    });
    setErrors(check.errors);
    if (!check.ok) return;
    const original: Record<string, string> = {
      company_id: String(employee.company_id), first_name: employee.first_name, last_name: employee.last_name, email: employee.email,
      department_id: String(employee.department_id), job_title_id: String(employee.job_title_id), job_level_id: String(employee.job_level_id), status: employee.status,
    };
    const changes: Record<string, string | number> = {};
    for (const [key, value] of Object.entries(form)) {
      if (value.trim() === original[key]) continue;
      changes[key] = key.endsWith("_id") ? Number(value) : value.trim();
    }
    if (Object.keys(changes).length === 0) {
      setErrors({ form: "Nothing has changed." });
      return;
    }
    void run(async () => {
      await api.patch<Employee>(`/employees/${employee.id}`, changes);
      onDone("Details saved.");
    });
  }

  return (
    <Modal title="Edit details" onClose={onClose} wide>
      <form onSubmit={submit} noValidate>
        <div className="modal-body">
          <p className="muted" style={{ marginTop: 0 }}>
            Pay, country and currency aren't edited here: pay changes are recorded as dated changes so the history stays intact.
          </p>
          {errors.form && <Alert kind="info">{errors.form}</Alert>}
          <div className="form-grid">
            <Field label="First name" required error={errors.first_name ?? error?.fields.first_name}>
              {(p) => <input {...p} value={form.first_name} onChange={set("first_name")} />}
            </Field>
            <Field label="Last name" required error={errors.last_name ?? error?.fields.last_name}>
              {(p) => <input {...p} value={form.last_name} onChange={set("last_name")} />}
            </Field>
            <Field label="Email" required error={errors.email ?? error?.fields.email}>
              {(p) => <input {...p} type="email" value={form.email} onChange={set("email")} />}
            </Field>
            <Field label="Company">
              {(p) => (
                <select {...p} value={form.company_id} onChange={set("company_id")}>
                  {ref.companies.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              )}
            </Field>
            <Field label="Department">
              {(p) => (
                <select {...p} value={form.department_id} onChange={set("department_id")}>
                  {ref.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select>
              )}
            </Field>
            <Field label="Job title">
              {(p) => (
                <select {...p} value={form.job_title_id} onChange={set("job_title_id")}>
                  {ref.jobTitles.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                </select>
              )}
            </Field>
            <Field label="Level">
              {(p) => (
                <select {...p} value={form.job_level_id} onChange={set("job_level_id")}>
                  {ref.jobLevels.map((l) => <option key={l.id} value={l.id}>{l.code}</option>)}
                </select>
              )}
            </Field>
            <Field label="Status" hint="To mark someone as terminated, use Terminate.">
              {(p) => (
                <select {...p} value={form.status} onChange={set("status")}>
                  <option value="active">Active</option>
                  <option value="on_leave">On leave</option>
                </select>
              )}
            </Field>
          </div>
          <FormError error={error} shownFields={["first_name", "last_name", "email"]} />
        </div>
        <Footer submitting={submitting} onClose={onClose} label="Save changes" />
      </form>
    </Modal>
  );
}

// --- record a compensation change (FR-4) ---

function describeOutcome(record: CompensationChange): string {
  return record.is_current
    ? "Change recorded and now current."
    : `Change recorded. It takes effect on ${formatDate(record.effective_date)}.`;
}

export function CompensationChangeModal({ profile, onClose, onDone }: ActionProps) {
  const ref = useLoadedReference();
  const { actor } = useActor();
  const employee = profile.employee;
  const base = ref.compensationTypes.find((t) => t.is_base_pay);
  const [form, setForm] = useState({
    type: String(base?.id ?? ref.compensationTypes[0]?.id ?? ""),
    reason: String(ref.changeReasons.find((r) => r.code === "annual_revision")?.id ?? ref.changeReasons[0]?.id ?? ""),
    date: todayIso(),
    amount: "",
    note: "",
    changed_by: actor,
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const { submitting, error, run } = useSubmit();
  const type = ref.compensationTypes.find((t) => String(t.id) === form.type);
  const set = (name: keyof typeof form) => (event: { target: { value: string } }) => setForm({ ...form, [name]: event.target.value });
  const future = form.date > todayIso();
  const current = profile.current.find((item) => String(item.compensation_type_id) === form.type);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const amount = parseAmount(form.amount, { positive: type?.is_base_pay });
    const check = collect({
      type: form.type ? undefined : "Choose a compensation type.",
      date: requireDate(form.date, "an effective date") ?? (form.date < employee.hire_date ? `Can't be before the hire date (${formatDate(employee.hire_date)}).` : undefined),
      amount: amount.error,
      changed_by: requireText(form.changed_by, "who is making this change"),
    });
    setErrors(check.errors);
    if (!check.ok) return;
    void run(async () => {
      const record = await api.post<CompensationChange>(`/employees/${employee.id}/compensation`, {
        compensation_type_id: Number(form.type),
        change_reason_id: Number(form.reason),
        effective_date: form.date,
        amount: amount.value,
        note: form.note.trim() || null,
        changed_by: form.changed_by.trim(),
      });
      onDone(describeOutcome(record));
    });
  }

  return (
    <Modal title="Record a compensation change" onClose={onClose} wide>
      <form onSubmit={submit} noValidate>
        <div className="modal-body">
          <p className="muted" style={{ marginTop: 0 }}>
            This adds a new record; earlier ones are never changed. To fix a mistake, record a new change with the reason “Correction”.
          </p>
          <div className="form-grid">
            <Field label="Compensation type" required error={errors.type}>
              {(p) => (
                <select {...p} value={form.type} onChange={set("type")}>
                  {ref.compensationTypes.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                </select>
              )}
            </Field>
            <Field label="Reason" required>
              {(p) => (
                <select {...p} value={form.reason} onChange={set("reason")}>
                  {ref.changeReasons.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
                </select>
              )}
            </Field>
            <Field
              label="Effective date"
              required
              error={errors.date ?? error?.fields.effective_date}
              hint={future ? "In the future: it is saved now and becomes current on that date." : undefined}
            >
              {(p) => <input {...p} type="date" min={employee.hire_date} value={form.date} onChange={set("date")} />}
            </Field>
            <Field
              label={`New amount (${employee.currency}${type ? ` ${perPeriod(type.period_months)}` : ""})`}
              required
              error={errors.amount ?? error?.fields.amount}
              hint={current ? `Currently ${current.amount} ${employee.currency} ${perPeriod(current.period_months)}.` : "No current amount for this type yet."}
            >
              {(p) => <input {...p} inputMode="decimal" value={form.amount} onChange={set("amount")} />}
            </Field>
          </div>
          <Field label="Note">
            {(p) => <textarea {...p} value={form.note} onChange={set("note")} placeholder="Optional context for the history" />}
          </Field>
          <Field label="Changed by" required error={errors.changed_by}>
            {(p) => <input {...p} value={form.changed_by} onChange={set("changed_by")} placeholder="your name or email" />}
          </Field>
          <FormError error={error} shownFields={["effective_date", "amount"]} />
        </div>
        <Footer submitting={submitting} onClose={onClose} label="Record change" />
      </form>
    </Modal>
  );
}

// --- relocation and currency change (FR-7) ---

function useAmounts(profile: Profile) {
  const [values, setValues] = useState<Record<number, string>>({});
  const set = (typeId: number, value: string) => setValues((previous) => ({ ...previous, [typeId]: value }));
  /** Parse every field; returns the payload or the errors by field. */
  function build(): { amounts?: { compensation_type_id: number; amount: string }[]; errors: Record<string, string> } {
    const errors: Record<string, string> = {};
    const amounts: { compensation_type_id: number; amount: string }[] = [];
    for (const item of profile.current) {
      const parsed = parseAmount(values[item.compensation_type_id] ?? "", { positive: item.is_base_pay });
      if (parsed.error) errors[`amount-${item.compensation_type_id}`] = parsed.error;
      else amounts.push({ compensation_type_id: item.compensation_type_id, amount: parsed.value! });
    }
    return { amounts: Object.keys(errors).length ? undefined : amounts, errors };
  }
  return { values, set, build };
}

export function RelocateModal({ profile, onClose, onDone }: ActionProps) {
  const ref = useLoadedReference();
  const { actor } = useActor();
  const employee = profile.employee;
  const [form, setForm] = useState({ country: "", date: todayIso(), note: "", changed_by: actor, newCurrency: false, currency: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const { submitting, error, run } = useSubmit();
  const amounts = useAmounts(profile);
  const set = (name: keyof typeof form) => (event: { target: { value: string } }) => setForm({ ...form, [name]: event.target.value });
  const destination = ref.countries.find((c) => c.code === form.country);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const built = form.newCurrency ? amounts.build() : { errors: {}, amounts: undefined };
    const check = collect({
      country: form.country ? undefined : "Choose the new country.",
      date: requireDate(form.date, "an effective date") ?? (form.date > todayIso() ? "Can't be in the future." : undefined),
      changed_by: requireText(form.changed_by, "who is making this change"),
      currency: form.newCurrency && !form.currency ? "Choose the new currency." : undefined,
      ...built.errors,
    });
    setErrors(check.errors);
    if (!check.ok) return;
    void run(async () => {
      const body: Record<string, unknown> = {
        country: form.country,
        effective_date: form.date,
        changed_by: form.changed_by.trim(),
        note: form.note.trim() || null,
      };
      if (form.newCurrency) {
        body.currency = form.currency;
        body.amounts = built.amounts;
      }
      await api.post<ChangeResult>(`/employees/${employee.id}/relocate`, body);
      onDone(`Moved to ${destination?.name ?? form.country}. The move is in their compensation history.`);
    });
  }

  return (
    <Modal title="Relocate" onClose={onClose} wide>
      <form onSubmit={submit} noValidate>
        <div className="modal-body">
          <p className="muted" style={{ marginTop: 0 }}>
            Moves {employee.first_name} from {employee.current_country} and records it in their history as a “Relocation” of base pay, with the same amount and currency.
          </p>
          <div className="form-grid">
            <Field label="New country" required error={errors.country ?? error?.fields.country}>
              {(p) => (
                <select {...p} value={form.country} onChange={(event) => {
                  const country = ref.countries.find((c) => c.code === event.target.value);
                  setForm({ ...form, country: event.target.value, currency: form.newCurrency && !form.currency ? country?.default_currency ?? "" : form.currency });
                }}>
                  <option value="">Choose…</option>
                  {ref.countries.filter((c) => c.code !== employee.current_country).map((c) => <option key={c.code} value={c.code}>{c.name} ({c.code})</option>)}
                </select>
              )}
            </Field>
            <Field label="Effective date" required error={errors.date ?? error?.fields.effective_date}>
              {(p) => <input {...p} type="date" max={todayIso()} value={form.date} onChange={set("date")} />}
            </Field>
          </div>
          <Field label="Note">
            {(p) => <textarea {...p} value={form.note} onChange={set("note")} />}
          </Field>
          <Field label="Changed by" required error={errors.changed_by}>
            {(p) => <input {...p} value={form.changed_by} onChange={set("changed_by")} placeholder="your name or email" />}
          </Field>
          <label className="check">
            <input
              type="checkbox"
              checked={form.newCurrency}
              onChange={(event) => setForm({ ...form, newCurrency: event.target.checked, currency: event.target.checked ? destination?.default_currency ?? form.currency : "" })}
            />
            <span>They will also be paid in a different currency from now on</span>
          </label>
          {form.newCurrency && (
            <>
              <Field label="New pay currency" required error={errors.currency}>
                {(p) => (
                  <select {...p} value={form.currency} onChange={set("currency")}>
                    <option value="">Choose…</option>
                    {ref.currencies.filter((c) => c.code !== employee.currency).map((c) => <option key={c.code} value={c.code}>{c.code} · {c.name}</option>)}
                  </select>
                )}
              </Field>
              <CurrencyAmounts items={profile.current} from={employee.currency} to={form.currency} values={amounts.values} onChange={amounts.set} errors={errors} />
            </>
          )}
          <FormError error={error} shownFields={["country", "effective_date"]} />
        </div>
        <Footer submitting={submitting} onClose={onClose} label={form.newCurrency ? "Relocate and change currency" : "Relocate"} />
      </form>
    </Modal>
  );
}

export function ChangeCurrencyModal({ profile, onClose, onDone }: ActionProps) {
  const ref = useLoadedReference();
  const { actor } = useActor();
  const employee = profile.employee;
  const [form, setForm] = useState({
    currency: "",
    reason: String(ref.changeReasons.find((r) => r.code === "market_adjustment")?.id ?? ref.changeReasons[0]?.id ?? ""),
    date: todayIso(),
    note: "",
    changed_by: actor,
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const { submitting, error, run } = useSubmit();
  const amounts = useAmounts(profile);
  const set = (name: keyof typeof form) => (event: { target: { value: string } }) => setForm({ ...form, [name]: event.target.value });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const built = amounts.build();
    const check = collect({
      currency: form.currency ? undefined : "Choose the new currency.",
      date: requireDate(form.date, "an effective date") ?? (form.date > todayIso() ? "Can't be in the future." : undefined),
      changed_by: requireText(form.changed_by, "who is making this change"),
      ...built.errors,
    });
    setErrors(check.errors);
    if (!check.ok) return;
    void run(async () => {
      await api.post<ChangeResult>(`/employees/${employee.id}/change-currency`, {
        currency: form.currency,
        amounts: built.amounts,
        change_reason_id: Number(form.reason),
        effective_date: form.date,
        changed_by: form.changed_by.trim(),
        note: form.note.trim() || null,
      });
      onDone(`Now paid in ${form.currency}. Every pay component was re-recorded in the new currency.`);
    });
  }

  return (
    <Modal title="Change pay currency" onClose={onClose} wide>
      <form onSubmit={submit} noValidate>
        <div className="modal-body">
          <Alert kind="info">
            Currently paid in {employee.currency}. Enter the new amount for <strong>every</strong> pay component in the new currency; they are saved together. Nothing is converted automatically.
          </Alert>
          <div className="form-grid">
            <Field label="New pay currency" required error={errors.currency ?? error?.fields.currency}>
              {(p) => (
                <select {...p} value={form.currency} onChange={set("currency")}>
                  <option value="">Choose…</option>
                  {ref.currencies.filter((c) => c.code !== employee.currency).map((c) => <option key={c.code} value={c.code}>{c.code} · {c.name}</option>)}
                </select>
              )}
            </Field>
            <Field label="Reason" required>
              {(p) => (
                <select {...p} value={form.reason} onChange={set("reason")}>
                  {ref.changeReasons.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
                </select>
              )}
            </Field>
            <Field label="Effective date" required error={errors.date ?? error?.fields.effective_date}>
              {(p) => <input {...p} type="date" max={todayIso()} value={form.date} onChange={set("date")} />}
            </Field>
          </div>
          <CurrencyAmounts items={profile.current} from={employee.currency} to={form.currency} values={amounts.values} onChange={amounts.set} errors={errors} />
          <Field label="Note">
            {(p) => <textarea {...p} value={form.note} onChange={set("note")} />}
          </Field>
          <Field label="Changed by" required error={errors.changed_by}>
            {(p) => <input {...p} value={form.changed_by} onChange={set("changed_by")} placeholder="your name or email" />}
          </Field>
          <FormError error={error} shownFields={["currency", "effective_date"]} />
        </div>
        <Footer submitting={submitting} onClose={onClose} label="Change currency" />
      </form>
    </Modal>
  );
}

// --- terminate ---

export function TerminateModal({ profile, onClose, onDone }: ActionProps) {
  const employee = profile.employee;
  const [date, setDate] = useState(todayIso());
  const [dateError, setDateError] = useState<string | undefined>();
  const { submitting, error, run } = useSubmit();

  function confirm() {
    const problem =
      requireDate(date, "the last day") ??
      (date < employee.hire_date ? `Can't be before the hire date (${formatDate(employee.hire_date)}).` : undefined) ??
      (date > todayIso() ? "Can't be in the future." : undefined);
    setDateError(problem);
    if (problem) return;
    void run(async () => {
      await api.post<Employee>(`/employees/${employee.id}/terminate`, { termination_date: date });
      onDone(`${employee.first_name} is now terminated as of ${formatDate(date)}.`);
    });
  }

  return (
    <ConfirmModal title="Terminate employee" confirmLabel="Terminate" onConfirm={confirm} onClose={onClose} danger busy={submitting} error={error}>
      <p style={{ marginTop: 0 }}>
        This marks <strong>{employee.first_name} {employee.last_name}</strong> as terminated and makes their record read-only. Nothing is deleted: they stay searchable and their history is kept. This can't be undone from here.
      </p>
      <Field label="Last day of employment" required error={dateError}>
        {(p) => <input {...p} type="date" min={employee.hire_date} max={todayIso()} value={date} onChange={(event) => setDate(event.target.value)} />}
      </Field>
    </ConfirmModal>
  );
}
