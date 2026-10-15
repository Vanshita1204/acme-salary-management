import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { API_URL, ApiError, api } from "../api/client";
import type { Company, ImportConfirmed, ValidationReport } from "../api/types";
import { Field, FormError, useSubmit } from "../components/forms";
import { Alert, Badge, PageHeader } from "../components/ui";
import { useActor, useLoadedReference, useReference } from "../lib/context";
import { formatDate, formatInteger, formatMoney } from "../lib/format";

type Step = "choose" | "review" | "done";

/** Upload → validation report → confirm (FR-5). Nothing is saved until the last step. */
export default function ImportPage() {
  const ref = useLoadedReference();
  const { refresh } = useReference();
  const { actor } = useActor();
  const [step, setStep] = useState<Step>("choose");
  const [file, setFile] = useState<File | undefined>();
  const [report, setReport] = useState<ValidationReport | undefined>();
  const [created, setCreated] = useState<ImportConfirmed | undefined>();
  const [changedBy, setChangedBy] = useState(actor);
  const [fieldError, setFieldError] = useState<string | undefined>();
  const input = useRef<HTMLInputElement>(null);
  const check = useSubmit();
  const save = useSubmit();

  const upload = (): FormData => {
    const form = new FormData();
    form.append("file", file!);
    return form;
  };

  function validate() {
    if (!file) {
      setFieldError("Choose a CSV file first.");
      return;
    }
    setFieldError(undefined);
    void check.run(async () => {
      setReport(await api.postForm<ValidationReport>("/import/validate", upload()));
      setStep("review");
    });
  }

  // The file names companies that don't exist yet. Only on request, so a typo in the file
  // doesn't quietly become a new legal entity: add them, then check the same file again.
  function addCompanies(names: string[]) {
    void check.run(async () => {
      for (const name of names) {
        try {
          await api.post<Company>("/companies", { name });
        } catch (caught) {
          if (!(caught instanceof ApiError && caught.status === 409)) throw caught; // already there
        }
      }
      refresh();
      setReport(await api.postForm<ValidationReport>("/import/validate", upload()));
    });
  }

  function confirm() {
    if (!changedBy.trim()) {
      setFieldError("Enter who is importing this file.");
      return;
    }
    setFieldError(undefined);
    void save.run(async () => {
      const form = upload();
      form.append("changed_by", changedBy.trim());
      try {
        setCreated(await api.postForm<ImportConfirmed>("/import/confirm", form));
        setStep("done");
      } catch (caught) {
        // The database may have changed since the preview; the server re-checks and
        // answers with a fresh report instead of saving anything.
        const body = caught instanceof ApiError ? (caught.body as Partial<ValidationReport> | null) : null;
        if (body && Array.isArray(body.errors)) {
          setReport(body as ValidationReport);
          throw new ApiError(422, "The file no longer passes validation, so nothing was imported. The updated problems are listed below.");
        }
        throw caught;
      }
    });
  }

  function reset() {
    setStep("choose");
    setFile(undefined);
    setReport(undefined);
    setCreated(undefined);
    setFieldError(undefined);
    if (input.current) input.current.value = "";
  }

  const lookup = {
    company: (id: number) => ref.companies.find((c) => c.id === id)?.name ?? String(id),
    department: (id: number) => ref.departments.find((d) => d.id === id)?.name ?? String(id),
    title: (id: number) => ref.jobTitles.find((t) => t.id === id)?.name ?? String(id),
    level: (id: number) => ref.jobLevels.find((l) => l.id === id)?.code ?? String(id),
  };

  return (
    <>
      <PageHeader
        title="Import employees"
        subtitle="Load many employees from a CSV file. The whole file is checked first; if any row is invalid, nothing is saved."
      />
      <ol className="steps" aria-label="Progress">
        <li className={step === "choose" ? "current" : "done"}>1. Choose file</li>
        <li className={step === "review" ? "current" : step === "done" ? "done" : undefined}>2. Review</li>
        <li className={step === "done" ? "current" : undefined}>3. Imported</li>
      </ol>

      {step === "choose" && (
        <section className="card">
          <h2>Choose a CSV file</h2>
          <p>
            <strong>1. Enter the reference data first.</strong> Departments, job titles, levels and compensation types must already exist under{" "}
            <Link to="/reference">Reference data</Link>, and a column that isn't a known compensation type is rejected. A company that doesn't exist yet can be added from the review step, after you confirm.
          </p>
          <p>
            <strong>2. Download the template.</strong> It has the employee fields, then one column per compensation type, so it always matches your current catalog.{" "}
            <a href={`${API_URL}/import/template`} download>Download the template</a>.
          </p>
          <p className="muted">
            The Base Pay column is required. Leave any other pay column blank for people who don't have it. Amounts are per payment period (monthly for allowances, annual for yearly bonuses), in the employee's currency. Names must match the lists (capital letters don't matter). Up to 10,000 rows per file.
          </p>
          <div className="dropzone">
            <Field label="CSV file" error={fieldError}>
              {(p) => (
                <input {...p} ref={input} type="file" accept=".csv,text/csv" onChange={(event) => setFile(event.target.files?.[0])} />
              )}
            </Field>
          </div>
          <FormError error={check.error} />
          <div className="actions" style={{ marginTop: "1rem" }}>
            <button type="button" className="primary" onClick={validate} disabled={check.submitting}>
              {check.submitting ? "Checking…" : "Check file"}
            </button>
          </div>
        </section>
      )}

      {step === "review" && report && (
        <>
          {report.ok ? (
            <Alert kind="success" title={`${formatInteger(report.row_count)} rows are valid`}>
              Nothing has been saved yet. Review the preview, then confirm to create these employees.
            </Alert>
          ) : (
            <Alert title={`${formatInteger(report.errors.length)} problem${report.errors.length === 1 ? "" : "s"} found: nothing will be imported`}>
              Fix the file and check it again. {formatInteger(report.row_count)} other row{report.row_count === 1 ? " is" : "s are"} fine.
            </Alert>
          )}
          <FormError error={save.error} />
          <FormError error={check.error} />

          {(report.unknown_companies?.length ?? 0) > 0 && (
            <section className="card" aria-label="Companies not found">
              <h2>{report.unknown_companies!.length === 1 ? "A company in this file doesn't exist yet" : "Some companies in this file don't exist yet"}</h2>
              <p>{report.unknown_companies!.map((name) => `“${name}”`).join(", ")}</p>
              <p className="muted">Check the spelling first: adding a misspelled name creates a new company. If they are right, add them and the file is checked again.</p>
              <div className="actions">
                <button type="button" className="primary" onClick={() => addCompanies(report.unknown_companies!)} disabled={check.submitting}>
                  {check.submitting ? "Adding…" : `Add ${report.unknown_companies!.length === 1 ? "this company" : `these ${report.unknown_companies!.length} companies`} and check again`}
                </button>
              </div>
            </section>
          )}

          {report.errors.length > 0 && (
            <section className="card">
              <h2>Problems</h2>
              <div className="table-wrap" tabIndex={0}>
                <table>
                  <thead>
                    <tr><th scope="col" className="num">Row</th><th scope="col">Column</th><th scope="col">Problem</th></tr>
                  </thead>
                  <tbody>
                    {report.errors.map((problem, index) => (
                      <tr key={index}>
                        <td className="num">{problem.row}</td>
                        <td>{problem.column || "—"}</td>
                        <td>{problem.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="muted">Row 1 is the header row, as in a spreadsheet.</p>
            </section>
          )}

          {report.preview.length > 0 && (
            <section className="card">
              <h2>Preview <Badge>{report.preview.length === report.row_count ? "all rows" : `first ${report.preview.length} of ${formatInteger(report.row_count)}`}</Badge></h2>
              <div className="table-wrap" tabIndex={0}>
                <table>
                  <thead>
                    <tr>
                      <th scope="col" className="num">Row</th><th scope="col">Name</th><th scope="col">Email</th><th scope="col">Company</th>
                      <th scope="col">Department</th><th scope="col">Title</th><th scope="col">Level</th><th scope="col">Country</th>
                      <th scope="col">Hire date</th><th scope="col" className="num">Base pay (monthly)</th><th scope="col">Other pay</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.preview.map((row) => (
                        <tr key={row.row}>
                          <td className="num">{row.row}</td>
                          <td>{row.first_name} {row.last_name}</td>
                          <td>{row.email}</td>
                          <td>{lookup.company(row.company_id)}</td>
                          <td>{lookup.department(row.department_id)}</td>
                          <td>{lookup.title(row.job_title_id)}</td>
                          <td>{lookup.level(row.job_level_id)}</td>
                          <td>{row.country}</td>
                          <td>{formatDate(row.hire_date)}</td>
                          <td className="num">{formatMoney(row.pay[0]?.amount, row.currency)}</td>
                          <td>
                            {row.pay.length > 1
                              ? row.pay.slice(1).map((p) => `${p.name}: ${formatMoney(p.amount, row.currency)}`).join("; ")
                              : "—"}
                          </td>
                        </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          <section className="card">
            {report.ok && (
              <Field label="Imported by" required error={fieldError} hint="Recorded on each employee's starting pay.">
                {(p) => <input {...p} value={changedBy} onChange={(event) => setChangedBy(event.target.value)} placeholder="your name or email" />}
              </Field>
            )}
            <div className="actions">
              <button type="button" onClick={reset} disabled={save.submitting}>Choose a different file</button>
              {report.ok && (
                <button type="button" className="primary" onClick={confirm} disabled={save.submitting}>
                  {save.submitting ? "Importing…" : `Import ${formatInteger(report.row_count)} employees`}
                </button>
              )}
            </div>
          </section>
        </>
      )}

      {step === "done" && created && (
        <section className="card">
          <Alert kind="success" title={`${formatInteger(created.created)} employees imported`}>
            Each has active records for the pay components in the file, dated on their hire date.
          </Alert>
          <div className="actions">
            <Link className="button primary" to="/">View employees</Link>
            <button type="button" onClick={reset}>Import another file</button>
          </div>
        </section>
      )}
    </>
  );
}
