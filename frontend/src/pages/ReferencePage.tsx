import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { ChangeReason, CompensationType, Department, JobLevel, JobTitle } from "../api/types";
import { Field, FormError, useSubmit } from "../components/forms";
import { Alert, Badge, EmptyState, PageHeader } from "../components/ui";
import { useLoadedReference, useReference } from "../lib/context";
import { periodLabel } from "../lib/format";
import { collect, requireText } from "../lib/validation";

const TABS = [
  { id: "types", label: "Compensation types" },
  { id: "reasons", label: "Change reasons" },
  { id: "departments", label: "Departments" },
  { id: "titles", label: "Job titles" },
  { id: "levels", label: "Levels" },
] as const;
type TabId = (typeof TABS)[number]["id"];

/** The lists HR maintains: shared by every employee, so entries are added, never deleted. */
export default function ReferencePage() {
  const ref = useLoadedReference();
  const [search, setSearch] = useSearchParams();
  const requested = search.get("tab");
  const tab: TabId = TABS.some((t) => t.id === requested) ? (requested as TabId) : "types";
  return (
    <>
      <PageHeader
        title="Reference data"
        subtitle="Lists used across the app. New entries are available everywhere immediately. Existing ones can't be removed, because pay history refers to them."
      />
      <div className="tabs" role="tablist" aria-label="Reference lists">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={t.id === tab}
            onClick={() => setSearch({ tab: t.id })}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" aria-label={TABS.find((t) => t.id === tab)?.label}>
        {tab === "types" && <CompensationTypes />}
        {tab === "reasons" && <ChangeReasons />}
        {tab === "departments" && <NamedList kind="department" path="/departments" items={ref.departments} />}
        {tab === "titles" && <NamedList kind="job title" path="/job-titles" items={ref.jobTitles} />}
        {tab === "levels" && <Levels />}
      </div>
    </>
  );
}

function Saved({ text }: { text: string | undefined }) {
  return text ? <Alert kind="success">{text}</Alert> : null;
}

// --- compensation types ---

const PERIODS = [
  { months: 1, label: "Monthly" },
  { months: 3, label: "Quarterly" },
  { months: 6, label: "Semi-annual" },
  { months: 12, label: "Annual" },
];

function CompensationTypes() {
  const ref = useLoadedReference();
  const { refresh } = useReference();
  const { submitting, error, run } = useSubmit();
  const [form, setForm] = useState({ name: "", category: "", subtype: "", period: "1", custom: "", counts: true });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<string | undefined>();
  const categories = [...new Set(ref.compensationTypes.map((t) => t.category))].sort();

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const months = form.period === "custom" ? Number(form.custom) : Number(form.period);
    const check = collect({
      name: requireText(form.name, "a name"),
      category: requireText(form.category, "a category"),
      period: Number.isInteger(months) && months > 0 && months <= 600 ? undefined : "Enter a whole number of months, 1 or more.",
    });
    setErrors(check.errors);
    if (!check.ok) return;
    setSaved(undefined);
    void run(async () => {
      const created = await api.post<CompensationType>("/compensation-types", {
        name: form.name.trim(),
        category: form.category.trim(),
        subtype: form.subtype.trim() || null,
        period_months: months,
        counts_toward_total: form.counts,
      });
      setForm({ name: "", category: "", subtype: "", period: "1", custom: "", counts: true });
      refresh();
      setSaved(`“${created.name}” is now available when recording pay changes.`);
    });
  }

  const err = (name: string) => errors[name] ?? error?.fields[name];
  return (
    <div className="grid grid-2">
      <section className="card">
        <h2>Compensation types ({ref.compensationTypes.length})</h2>
        <div className="table-wrap" tabIndex={0}>
          <table>
            <thead>
              <tr><th scope="col">Name</th><th scope="col">Category</th><th scope="col">Subtype</th><th scope="col">Paid</th><th scope="col">Counts toward total</th></tr>
            </thead>
            <tbody>
              {ref.compensationTypes.map((t) => (
                <tr key={t.id}>
                  <td>{t.name} {t.is_base_pay && <Badge tone="info">Base pay</Badge>}</td>
                  <td>{t.category}</td>
                  <td>{t.subtype ?? "—"}</td>
                  <td>{periodLabel(t.period_months)}</td>
                  <td>{t.counts_toward_total ? "Yes" : <Badge>Outside total</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <form className="card" onSubmit={submit} noValidate aria-label="Add compensation type">
        <h2>Add a compensation type</h2>
        <Saved text={saved} />
        <Field label="Name" required error={err("name")} hint="e.g. Spot Bonus">
          {(p) => <input {...p} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
        </Field>
        <Field label="Category" required error={err("category")} hint="Reuse an existing category so reports group correctly; type a new one if needed.">
          {(p) => (
            <>
              <input {...p} list="category-options" value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} />
              <datalist id="category-options">{categories.map((c) => <option key={c} value={c} />)}</datalist>
            </>
          )}
        </Field>
        <Field label="Subtype" error={err("subtype")} hint="Optional, e.g. housing. A category and subtype pair can only be used once.">
          {(p) => <input {...p} value={form.subtype} onChange={(e) => setForm({ ...form, subtype: e.target.value })} />}
        </Field>
        <Field label="How often it is paid" required error={err("period") ?? err("period_months")}>
          {(p) => (
            <select {...p} value={form.period} onChange={(e) => setForm({ ...form, period: e.target.value })}>
              {PERIODS.map((o) => <option key={o.months} value={o.months}>{o.label}</option>)}
              <option value="custom">Custom number of months…</option>
            </select>
          )}
        </Field>
        {form.period === "custom" && (
          <Field label="Months between payments" required>
            {(p) => <input {...p} inputMode="numeric" value={form.custom} onChange={(e) => setForm({ ...form, custom: e.target.value })} />}
          </Field>
        )}
        <label className="check">
          <input type="checkbox" checked={form.counts} onChange={(e) => setForm({ ...form, counts: e.target.checked })} />
          <span>Counts toward total compensation (CTC). Turn off for pay the employer gives outside CTC, such as some reimbursements.</span>
        </label>
        <FormError error={error} shownFields={["name", "category", "subtype", "period_months"]} />
        <button type="submit" className="primary" disabled={submitting}>{submitting ? "Adding…" : "Add type"}</button>
      </form>
    </div>
  );
}

// --- change reasons ---

const slug = (text: string) => text.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

function ChangeReasons() {
  const ref = useLoadedReference();
  const { refresh } = useReference();
  const { submitting, error, run } = useSubmit();
  const [form, setForm] = useState({ label: "", code: "" });
  const [codeEdited, setCodeEdited] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<string | undefined>();

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const check = collect({
      label: requireText(form.label, "a label"),
      code: /^[a-z][a-z0-9_]*$/.test(form.code) ? undefined : "Use lowercase letters, digits and underscores, starting with a letter.",
    });
    setErrors(check.errors);
    if (!check.ok) return;
    setSaved(undefined);
    void run(async () => {
      const created = await api.post<ChangeReason>("/change-reasons", { code: form.code, label: form.label.trim() });
      setForm({ label: "", code: "" });
      setCodeEdited(false);
      refresh();
      setSaved(`“${created.label}” can now be used for any pay change.`);
    });
  }

  const err = (name: string) => errors[name] ?? error?.fields[name];
  return (
    <div className="grid grid-2">
      <section className="card">
        <h2>Change reasons ({ref.changeReasons.length})</h2>
        <p className="muted">Any reason can be used with any compensation type.</p>
        <div className="table-wrap" tabIndex={0}>
          <table>
            <thead><tr><th scope="col">Label</th><th scope="col">Code</th></tr></thead>
            <tbody>
              {ref.changeReasons.map((r) => (
                <tr key={r.id}><td>{r.label}</td><td>{r.code}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <form className="card" onSubmit={submit} noValidate aria-label="Add change reason">
        <h2>Add a reason</h2>
        <Saved text={saved} />
        <Field label="Label" required error={err("label")} hint="What people see, e.g. Spot award">
          {(p) => (
            <input {...p} value={form.label} onChange={(e) => setForm({ label: e.target.value, code: codeEdited ? form.code : slug(e.target.value) })} />
          )}
        </Field>
        <Field label="Code" required error={err("code")} hint="Short identifier used in reports, e.g. spot_award. Filled in from the label.">
          {(p) => <input {...p} value={form.code} onChange={(e) => { setCodeEdited(true); setForm({ ...form, code: e.target.value }); }} />}
        </Field>
        <FormError error={error} shownFields={["label", "code"]} />
        <button type="submit" className="primary" disabled={submitting}>{submitting ? "Adding…" : "Add reason"}</button>
      </form>
    </div>
  );
}

// --- departments and job titles ---

function NamedList({ kind, path, items }: { kind: string; path: string; items: (Department | JobTitle)[] }) {
  const { refresh } = useReference();
  const { submitting, error, run } = useSubmit();
  const [name, setName] = useState("");
  const [problem, setProblem] = useState<string | undefined>();
  const [saved, setSaved] = useState<string | undefined>();

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const missing = requireText(name, "a name");
    setProblem(missing);
    if (missing) return;
    setSaved(undefined);
    void run(async () => {
      const created = await api.post<Department>(path, { name: name.trim() });
      setName("");
      refresh();
      setSaved(`“${created.name}” added.`);
    });
  }

  return (
    <div className="grid grid-2">
      <section className="card">
        <h2>{kind[0].toUpperCase() + kind.slice(1)}s ({items.length})</h2>
        {items.length === 0 ? (
          <EmptyState>None yet.</EmptyState>
        ) : (
          <ul>{items.map((item) => <li key={item.id}>{item.name}</li>)}</ul>
        )}
      </section>
      <form className="card" onSubmit={submit} noValidate aria-label={`Add ${kind}`}>
        <h2>Add a {kind}</h2>
        <Saved text={saved} />
        <Field label="Name" required error={problem ?? error?.fields.name} hint="Capital letters don't make a new entry: “engineering” is the same as “Engineering”.">
          {(p) => <input {...p} value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <FormError error={error} shownFields={["name"]} />
        <button type="submit" className="primary" disabled={submitting}>{submitting ? "Adding…" : `Add ${kind}`}</button>
      </form>
    </div>
  );
}

// --- levels ---

function Levels() {
  const ref = useLoadedReference();
  const { refresh } = useReference();
  const { submitting, error, run } = useSubmit();
  const nextRank = Math.max(0, ...ref.jobLevels.map((l) => l.rank)) + 1;
  const [form, setForm] = useState({ code: "", label: "", rank: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<string | undefined>();

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const rank = Number(form.rank || nextRank);
    const check = collect({
      code: requireText(form.code, "a code"),
      label: requireText(form.label, "a label"),
      rank: Number.isInteger(rank) ? undefined : "Enter a whole number.",
    });
    setErrors(check.errors);
    if (!check.ok) return;
    setSaved(undefined);
    void run(async () => {
      const created = await api.post<JobLevel>("/job-levels", { code: form.code.trim(), label: form.label.trim(), rank });
      setForm({ code: "", label: "", rank: "" });
      refresh();
      setSaved(`Level ${created.code} added.`);
    });
  }

  const err = (name: string) => errors[name] ?? error?.fields[name];
  return (
    <div className="grid grid-2">
      <section className="card">
        <h2>Levels ({ref.jobLevels.length})</h2>
        <p className="muted">Listed from most junior to most senior. The rank sets the order.</p>
        <div className="table-wrap" tabIndex={0}>
          <table>
            <thead><tr><th scope="col" className="num">Rank</th><th scope="col">Code</th><th scope="col">Label</th></tr></thead>
            <tbody>
              {ref.jobLevels.map((l) => (
                <tr key={l.id}><td className="num">{l.rank}</td><td>{l.code}</td><td>{l.label}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <form className="card" onSubmit={submit} noValidate aria-label="Add level">
        <h2>Add a level</h2>
        <Saved text={saved} />
        <Field label="Code" required error={err("code")} hint="e.g. L8">{(p) => <input {...p} value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} />}</Field>
        <Field label="Label" required error={err("label")} hint="e.g. Level 8">{(p) => <input {...p} value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} />}</Field>
        <Field label="Rank" error={err("rank")} hint={`Higher is more senior. Left blank it goes after the last level (${nextRank}).`}>
          {(p) => <input {...p} inputMode="numeric" value={form.rank} onChange={(e) => setForm({ ...form, rank: e.target.value })} placeholder={String(nextRank)} />}
        </Field>
        <FormError error={error} shownFields={["code", "label", "rank"]} />
        <button type="submit" className="primary" disabled={submitting}>{submitting ? "Adding…" : "Add level"}</button>
      </form>
    </div>
  );
}
