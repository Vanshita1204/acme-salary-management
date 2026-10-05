import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { HistoryItem, Profile } from "../api/types";
import {
  ChangeCurrencyModal,
  CompensationChangeModal,
  EditEmployeeModal,
  RelocateModal,
  TerminateModal,
} from "../components/EmployeeActions";
import { Alert, AsyncView, Badge, PageHeader, RatesNote } from "../components/ui";
import { useLoadedReference, useReporting } from "../lib/context";
import { formatAmount, formatDate, formatMoney, formatPercent, fullName, periodLabel, STATUS_LABELS } from "../lib/format";
import { useAsync } from "../lib/useAsync";

type Action = "edit" | "change" | "relocate" | "currency" | "terminate";

export default function EmployeePage() {
  const { id } = useParams();
  const { currency } = useReporting();
  // A hand-typed address like /employees/abc is simply not an employee: don't ask the server.
  const valid = /^[1-9]\d{0,17}$/.test(id ?? "");
  const state = useAsync(
    () => (valid ? api.get<Profile>(`/employees/${id}`, { reporting_currency: currency }) : Promise.reject(new ApiError(404, "There is no employee with that number."))),
    [id, currency],
  );
  const [action, setAction] = useState<Action | undefined>();
  const [message, setMessage] = useState<string | undefined>();

  return (
    <AsyncView state={state}>
      {(profile) => {
        const person = profile.employee;
        const terminated = person.status === "terminated";
        const done = (text: string) => {
          setAction(undefined);
          setMessage(text);
          state.reload();
        };
        const modalProps = { profile, onClose: () => setAction(undefined), onDone: done };
        return (
          <>
            <p className="muted"><Link to="/">← All employees</Link></p>
            <PageHeader
              title={fullName(person)}
              subtitle={
                <>
                  {person.code} · {person.job_title}, {person.job_level} · {person.department}{" "}
                  <Badge tone={person.status === "active" ? "good" : person.status === "on_leave" ? "warning" : "neutral"}>
                    {STATUS_LABELS[person.status]}
                  </Badge>
                </>
              }
              actions={
                terminated ? undefined : (
                  <>
                    <button type="button" className="primary" onClick={() => setAction("change")}>Record pay change</button>
                    <button type="button" onClick={() => setAction("edit")}>Edit details</button>
                    <button type="button" onClick={() => setAction("relocate")}>Relocate</button>
                    <button type="button" onClick={() => setAction("currency")}>Change currency</button>
                    <button type="button" onClick={() => setAction("terminate")}>Terminate…</button>
                  </>
                )
              }
            />
            {message && <Alert kind="success">{message}</Alert>}
            {terminated && (
              <Alert kind="info" title={`Terminated on ${formatDate(person.termination_date)}`}>
                This record is read-only. Their history stays searchable and is never deleted.
              </Alert>
            )}

            <section className="card">
              <h2>Details</h2>
              <dl className="detail-grid">
                <Detail label="Email" value={<a href={`mailto:${person.email}`}>{person.email}</a>} />
                <Detail label="Company" value={<CompanyName id={person.company_id} />} />
                <Detail label="Country" value={person.current_country} />
                <Detail label="Pay currency" value={person.currency} />
                <Detail label="Hired" value={formatDate(person.hire_date)} />
                {person.termination_date && <Detail label="Terminated" value={formatDate(person.termination_date)} />}
              </dl>
            </section>

            <section className="card">
              <h2>Current compensation</h2>
              <div className="table-wrap" tabIndex={0}>
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Component</th>
                      <th scope="col">Category</th>
                      <th scope="col">Paid</th>
                      <th scope="col" className="num">Amount ({person.currency})</th>
                      <th scope="col" className="num">Per year ({person.currency})</th>
                      <th scope="col" className="num">Per year ({profile.reporting_currency})</th>
                      <th scope="col">Since</th>
                    </tr>
                  </thead>
                  <tbody>
                    {profile.current.map((item) => (
                      <tr key={item.compensation_type_id}>
                        <td>
                          {item.name} {item.is_base_pay && <Badge tone="info">Base pay</Badge>}{" "}
                          {!item.counts_toward_total && <Badge>Outside total</Badge>}
                        </td>
                        <td>{item.category}</td>
                        <td>{periodLabel(item.period_months)}</td>
                        <td className="num">{formatAmount(item.amount, person.currency)}</td>
                        <td className="num">{formatAmount(item.annual_amount, person.currency)}</td>
                        <td className="num">{formatAmount(item.annual_amount_reporting, profile.reporting_currency)}</td>
                        <td>{formatDate(item.effective_date)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="total-line">
                <span>Total compensation (CTC) per year</span>
                <strong>{formatMoney(profile.total_compensation, person.currency)}</strong>
                {person.currency !== profile.reporting_currency && (
                  <strong>{formatMoney(profile.total_compensation_reporting, profile.reporting_currency)}</strong>
                )}
              </div>
              <RatesNote ratesAsOf={profile.rates_as_of} />
            </section>

            <section className="card">
              <h2>Compensation history</h2>
              <p className="muted">Newest first. Records are never edited; a mistake is fixed with a new “Correction”.</p>
              <div className="table-wrap" tabIndex={0}>
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Effective</th>
                      <th scope="col">Component</th>
                      <th scope="col">Reason</th>
                      <th scope="col" className="num">Amount</th>
                      <th scope="col" className="num">Previous</th>
                      <th scope="col" className="num">Change</th>
                      <th scope="col">Note</th>
                      <th scope="col">By</th>
                    </tr>
                  </thead>
                  <tbody>
                    {profile.history.map((item, index) => (
                      <tr key={item.record_id}>
                        <td>{formatDate(item.effective_date)}</td>
                        <td>{item.compensation_type}</td>
                        <td>{item.change_reason_label}</td>
                        <td className="num">{formatMoney(item.amount, item.currency)}</td>
                        <td className="num">{item.previous_amount === null ? "—" : formatMoney(item.previous_amount, previousCurrency(profile.history, index))}</td>
                        <td className="num">
                          {item.currency_changed ? (
                            <span title="The currency changed, so the two amounts can't be compared.">
                              <Badge tone="warning">Currency changed</Badge>
                            </span>
                          ) : (
                            formatPercent(item.percent_change)
                          )}
                        </td>
                        <td>{item.note ?? ""}</td>
                        <td>{item.changed_by}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            {action === "edit" && <EditEmployeeModal {...modalProps} />}
            {action === "change" && <CompensationChangeModal {...modalProps} />}
            {action === "relocate" && <RelocateModal {...modalProps} />}
            {action === "currency" && <ChangeCurrencyModal {...modalProps} />}
            {action === "terminate" && <TerminateModal {...modalProps} />}
          </>
        );
      }}
    </AsyncView>
  );
}

/** The currency of the earlier record of the same component (it differs after a currency change). */
function previousCurrency(history: HistoryItem[], index: number): string {
  const item = history[index];
  const earlier = history.slice(index + 1).find((other) => other.compensation_type_id === item.compensation_type_id);
  return earlier?.currency ?? item.currency;
}

function Detail({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function CompanyName({ id }: { id: number }) {
  const ref = useLoadedReference();
  return <>{ref.companies.find((company) => company.id === id)?.name ?? `#${id}`}</>;
}
