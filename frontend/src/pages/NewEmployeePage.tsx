import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Employee } from "../api/types";
import { Field, FormError, useSubmit } from "../components/forms";
import { PageHeader } from "../components/ui";
import { useActor, useLoadedReference } from "../lib/context";
import { perPeriod, todayIso } from "../lib/format";
import { collect, parseAmount, requireDate, requireText, validEmail } from "../lib/validation";

export default function NewEmployeePage() {
  const ref = useLoadedReference();
  const { actor } = useActor();
  const navigate = useNavigate();
  const { submitting, error, run } = useSubmit();
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [form, setForm] = useState({
    first_name: "", last_name: "", email: "", company_id: "", department_id: "", job_title_id: "", job_level_id: "",
    current_country: "", currency: "", status: "active", hire_date: todayIso(), base_pay: "", note: "", changed_by: actor,
  });
  const set = (name: keyof typeof form) => (event: { target: { value: string } }) => setForm({ ...form, [name]: event.target.value });
  const baseType = ref.compensationTypes.find((t) => t.is_base_pay);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const pay = parseAmount(form.base_pay, { positive: true });
    const check = collect({
      first_name: requireText(form.first_name, "a first name"),
      last_name: requireText(form.last_name, "a last name"),
      email: validEmail(form.email),
      company_id: form.company_id ? undefined : "Choose a company.",
      department_id: form.department_id ? undefined : "Choose a department.",
      job_title_id: form.job_title_id ? undefined : "Choose a job title.",
      job_level_id: form.job_level_id ? undefined : "Choose a level.",
      current_country: form.current_country ? undefined : "Choose a country.",
      currency: form.currency ? undefined : "Choose the pay currency.",
      hire_date: requireDate(form.hire_date, "a hire date"),
      base_pay: pay.error,
      changed_by: requireText(form.changed_by, "who is adding this employee"),
    });
    setErrors(check.errors);
    if (!check.ok) return;
    void run(async () => {
      const created = await api.post<Employee>("/employees", {
        first_name: form.first_name.trim(),
        last_name: form.last_name.trim(),
        email: form.email.trim(),
        company_id: Number(form.company_id),
        department_id: Number(form.department_id),
        job_title_id: Number(form.job_title_id),
        job_level_id: Number(form.job_level_id),
        current_country: form.current_country,
        currency: form.currency,
        status: form.status,
        hire_date: form.hire_date,
        base_pay: { amount: pay.value, note: form.note.trim() || null },
        changed_by: form.changed_by.trim(),
      });
      navigate(`/employees/${created.id}`);
    });
  }

  const err = (name: string) => errors[name] ?? error?.fields[name];
  return (
    <>
      <p className="muted"><Link to="/">← All employees</Link></p>
      <PageHeader title="Add employee" subtitle="Every employee starts with a base pay record dated on their hire date." />
      <form className="card" onSubmit={submit} noValidate>
        <div className="form-grid">
          <Field label="First name" required error={err("first_name")}>{(p) => <input {...p} value={form.first_name} onChange={set("first_name")} />}</Field>
          <Field label="Last name" required error={err("last_name")}>{(p) => <input {...p} value={form.last_name} onChange={set("last_name")} />}</Field>
          <Field label="Email" required error={err("email")}>{(p) => <input {...p} type="email" value={form.email} onChange={set("email")} />}</Field>
          <Field label="Company" required error={err("company_id")}>
            {(p) => (
              <select {...p} value={form.company_id} onChange={set("company_id")}>
                <option value="">Choose…</option>
                {ref.companies.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            )}
          </Field>
          <Field label="Department" required error={err("department_id")}>
            {(p) => (
              <select {...p} value={form.department_id} onChange={set("department_id")}>
                <option value="">Choose…</option>
                {ref.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
            )}
          </Field>
          <Field label="Job title" required error={err("job_title_id")}>
            {(p) => (
              <select {...p} value={form.job_title_id} onChange={set("job_title_id")}>
                <option value="">Choose…</option>
                {ref.jobTitles.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            )}
          </Field>
          <Field label="Level" required error={err("job_level_id")}>
            {(p) => (
              <select {...p} value={form.job_level_id} onChange={set("job_level_id")}>
                <option value="">Choose…</option>
                {ref.jobLevels.map((l) => <option key={l.id} value={l.id}>{l.code}</option>)}
              </select>
            )}
          </Field>
          <Field label="Country" required error={err("current_country")}>
            {(p) => (
              <select
                {...p}
                value={form.current_country}
                onChange={(event) => {
                  const country = ref.countries.find((c) => c.code === event.target.value);
                  // The country's usual currency is a starting point; it can be anything.
                  setForm({ ...form, current_country: event.target.value, currency: form.currency || (country?.default_currency ?? "") });
                }}
              >
                <option value="">Choose…</option>
                {ref.countries.map((c) => <option key={c.code} value={c.code}>{c.name} ({c.code})</option>)}
              </select>
            )}
          </Field>
          <Field label="Pay currency" required error={err("currency")} hint="Independent of country: someone in Dubai can be paid in USD.">
            {(p) => (
              <select {...p} value={form.currency} onChange={set("currency")}>
                <option value="">Choose…</option>
                {ref.currencies.map((c) => <option key={c.code} value={c.code}>{c.code} · {c.name}</option>)}
              </select>
            )}
          </Field>
          <Field label="Status">
            {(p) => (
              <select {...p} value={form.status} onChange={set("status")}>
                <option value="active">Active</option>
                <option value="on_leave">On leave</option>
              </select>
            )}
          </Field>
          <Field label="Hire date" required error={err("hire_date")}>{(p) => <input {...p} type="date" value={form.hire_date} onChange={set("hire_date")} />}</Field>
          <Field
            label={`Base pay (${form.currency || "currency"}${baseType ? ` ${perPeriod(baseType.period_months)}` : ""})`}
            required
            error={err("base_pay") ?? err("base_pay.amount")}
          >
            {(p) => <input {...p} inputMode="decimal" value={form.base_pay} onChange={set("base_pay")} />}
          </Field>
        </div>
        <Field label="Note on the starting pay">{(p) => <textarea {...p} value={form.note} onChange={set("note")} placeholder="Optional, e.g. offer letter reference" />}</Field>
        <Field label="Added by" required error={err("changed_by")}>{(p) => <input {...p} value={form.changed_by} onChange={set("changed_by")} placeholder="your name or email" />}</Field>
        <FormError error={error} shownFields={Object.keys(form)} />
        <div className="actions">
          <button type="submit" className="primary" disabled={submitting}>{submitting ? "Saving…" : "Add employee"}</button>
          <Link className="button" to="/">Cancel</Link>
        </div>
      </form>
    </>
  );
}
