import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Params } from "../api/client";
import type {
  ChangeReport, Composition, CostRow, GroupBy, Histogram, Outliers, RoleByCountry, Stats, Summary,
} from "../api/types";
import { BarList, ChartCard, HistogramChart } from "../components/charts";
import { FilterBar } from "../components/FilterBar";
import { Field } from "../components/forms";
import { AsyncView, Badge, EmptyState, PageHeader, RatesNote, StatTile } from "../components/ui";
import { useLoadedReference, useReporting } from "../lib/context";
import {
  formatAmount, formatCompact, formatDate, formatInteger, formatMoney, formatPercent, formatShare, fullName, todayIso,
} from "../lib/format";
import { useAsync } from "../lib/useAsync";
import { filterParams, parseView, viewToSearch, type View } from "../lib/view";

const GROUPS: { value: GroupBy; label: string }[] = [
  { value: "department", label: "Department" },
  { value: "country", label: "Country" },
  { value: "title", label: "Job title" },
  { value: "level", label: "Level" },
];

const TOP = 10;

/** The questions in §5, answered for active employees' current pay in the reporting currency. */
export default function AnalyticsPage() {
  const [search, setSearch] = useSearchParams();
  const { currency } = useReporting();
  const view = useMemo(() => parseView(search), [search]);
  const change = useCallback(
    (changes: Partial<View>) => setSearch(viewToSearch({ ...parseView(search), ...changes, statuses: [] })),
    [search, setSearch],
  );
  // Same filters as the directory; pay insights always cover active employees only.
  const base: Params = { ...filterParams(view), reporting_currency: currency };
  const key = `${search.toString()}|${currency}`;

  return (
    <>
      <PageHeader
        title="Pay insights"
        subtitle={`Active employees' current pay, in ${currency}. Filters apply to every view below.`}
      />
      <div className="card">
        <FilterBar view={view} onChange={change} />
      </div>
      <SummarySection base={base} depKey={key} currency={currency} />
      <StatsSection base={base} depKey={key} currency={currency} />
      <RoleSection base={base} depKey={key} currency={currency} />
      <DistributionSection base={base} depKey={key} currency={currency} />
      <OutlierSection base={base} depKey={key} currency={currency} />
      <CompositionSection base={base} depKey={key} currency={currency} />
      <ChangesSection base={filterParams(view)} depKey={search.toString()} />
    </>
  );
}

interface SectionProps { base: Params; depKey: string; currency: string }

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <section className="card" aria-label={title}>
      <h2>{title}</h2>
      {subtitle && <p className="muted" style={{ marginTop: 0 }}>{subtitle}</p>}
      {children}
    </section>
  );
}

function costBars(rows: CostRow[], currency: string) {
  return rows.slice(0, TOP).map((row) => ({
    key: row.key,
    label: row.label,
    value: Number(row.total_cost),
    display: formatCompact(row.total_cost, currency),
    detail: `${formatMoney(row.total_cost, currency)} · ${formatInteger(row.headcount)} employees · ${formatShare(row.share)} of total`,
  }));
}

function CostTable({ rows, currency, what }: { rows: CostRow[]; currency: string; what: string }) {
  return (
    <div className="table-wrap" tabIndex={0}>
      <table>
        <thead>
          <tr><th scope="col">{what}</th><th scope="col" className="num">Employees</th><th scope="col" className="num">Annual cost ({currency})</th><th scope="col" className="num">Share</th></tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              <td>{row.label}</td>
              <td className="num">{formatInteger(row.headcount)}</td>
              <td className="num">{formatAmount(row.total_cost, currency)}</td>
              <td className="num">{formatShare(row.share)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// 1. What does ACME spend, overall and by country and department?
function SummarySection({ base, depKey, currency }: SectionProps) {
  const state = useAsync(() => api.get<Summary>("/analytics/summary", base), [depKey]);
  return (
    <Section title="Spend" subtitle="Total annual compensation cost, overall and by country and department.">
      <AsyncView state={state}>
        {(s) => (
          <>
            <div className="tiles">
              <StatTile label="Employees" value={formatInteger(s.headcount)} />
              <StatTile label="Total annual cost" value={formatCompact(s.total_cost, currency)} note={formatMoney(s.total_cost, currency)} />
              <StatTile label="Average per employee" value={formatCompact(s.average, currency)} note={formatMoney(s.average, currency)} />
              <StatTile label="Median per employee" value={formatCompact(s.median, currency)} note={formatMoney(s.median, currency)} />
            </div>
            {s.headcount === 0 ? (
              <EmptyState>No active employees with pay match these filters.</EmptyState>
            ) : (
              <div className="grid grid-2">
                <ChartCard title="Cost by country" subtitle={`Largest ${Math.min(TOP, s.by_country.length)} of ${s.by_country.length}, in ${currency}`} table={<CostTable rows={s.by_country} currency={currency} what="Country" />}>
                  <BarList rows={costBars(s.by_country, currency)} ariaLabel="Annual cost by country" />
                </ChartCard>
                <ChartCard title="Cost by department" subtitle={`In ${currency}`} table={<CostTable rows={s.by_department} currency={currency} what="Department" />}>
                  <BarList rows={costBars(s.by_department, currency)} ariaLabel="Annual cost by department" />
                </ChartCard>
              </div>
            )}
            <RatesNote ratesAsOf={s.rates_as_of} excluded={s.excluded_no_rate} currency={s.reporting_currency} />
          </>
        )}
      </AsyncView>
    </Section>
  );
}

// 2. Average, median, minimum and maximum pay by department, country, title, level
function StatsSection({ base, depKey, currency }: SectionProps) {
  const [groupBy, setGroupBy] = useState<GroupBy>("department");
  const state = useAsync(() => api.get<Stats>("/analytics/stats", { ...base, group_by: groupBy }), [depKey, groupBy]);
  return (
    <Section title="Pay by group" subtitle="Average, median, lowest and highest annual pay.">
      <Field label="Group by">
        {(p) => (
          <select {...p} value={groupBy} onChange={(event) => setGroupBy(event.target.value as GroupBy)} style={{ maxWidth: "14rem" }}>
            {GROUPS.map((g) => <option key={g.value} value={g.value}>{g.label}</option>)}
          </select>
        )}
      </Field>
      <AsyncView state={state}>
        {(stats) =>
          stats.groups.length === 0 ? (
            <EmptyState>No data for these filters.</EmptyState>
          ) : (
            <>
              <div className="table-wrap" tabIndex={0}>
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{GROUPS.find((g) => g.value === groupBy)?.label}</th>
                      <th scope="col" className="num">Employees</th>
                      <th scope="col" className="num">Average</th>
                      <th scope="col" className="num">Median</th>
                      <th scope="col" className="num">Lowest</th>
                      <th scope="col" className="num">Highest</th>
                    </tr>
                  </thead>
                  <tbody>
                    {stats.groups.map((row) => (
                      <tr key={row.key}>
                        <td>{row.label}</td>
                        <td className="num">{formatInteger(row.headcount)}</td>
                        <td className="num">{formatAmount(row.average, currency)}</td>
                        <td className="num">{formatAmount(row.median, currency)}</td>
                        <td className="num">{formatAmount(row.minimum, currency)}</td>
                        <td className="num">{formatAmount(row.maximum, currency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <RatesNote ratesAsOf={stats.rates_as_of} excluded={stats.excluded_no_rate} currency={stats.reporting_currency} />
            </>
          )
        }
      </AsyncView>
    </Section>
  );
}

// 3. How does pay for the same role and level differ across countries?
function RoleSection({ base, depKey, currency }: SectionProps) {
  const ref = useLoadedReference();
  const [titleId, setTitleId] = useState(String(ref.jobTitles[0]?.id ?? ""));
  // Start from a middle level: a title across every level and country is hundreds of rows.
  const [levelId, setLevelId] = useState(String(ref.jobLevels[Math.floor((ref.jobLevels.length - 1) / 2)]?.id ?? ""));
  // This view has its own title/level choice; the filter bar's title/level don't apply.
  const params = { ...base, job_title_id: titleId, job_level_id: levelId || undefined };
  const state = useAsync(() => api.get<RoleByCountry>("/analytics/role-by-country", params), [depKey, titleId, levelId]);
  return (
    <Section title="Same role across countries" subtitle="Pick a job title, and optionally a level, to compare countries.">
      <div className="form-grid">
        <Field label="Job title">
          {(p) => (
            <select {...p} value={titleId} onChange={(event) => setTitleId(event.target.value)}>
              {ref.jobTitles.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          )}
        </Field>
        <Field label="Level">
          {(p) => (
            <select {...p} value={levelId} onChange={(event) => setLevelId(event.target.value)}>
              <option value="">All levels</option>
              {ref.jobLevels.map((l) => <option key={l.id} value={l.id}>{l.code}</option>)}
            </select>
          )}
        </Field>
      </div>
      <AsyncView state={state}>
        {(data) =>
          data.rows.length === 0 ? (
            <EmptyState>Nobody matches this title and level.</EmptyState>
          ) : (
            <>
              <div className="table-wrap" tabIndex={0}>
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Level</th><th scope="col">Country</th>
                      <th scope="col" className="num">Employees</th>
                      <th scope="col" className="num">Average ({currency})</th>
                      <th scope="col" className="num">Median ({currency})</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.rows.map((row) => (
                      <tr key={`${row.job_level_id}-${row.country}`}>
                        <td>{row.job_level}</td><td>{row.country}</td>
                        <td className="num">{formatInteger(row.headcount)}</td>
                        <td className="num">{formatAmount(row.average, currency)}</td>
                        <td className="num">{formatAmount(row.median, currency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <RatesNote ratesAsOf={data.rates_as_of} excluded={data.excluded_no_rate} currency={data.reporting_currency} />
            </>
          )
        }
      </AsyncView>
    </Section>
  );
}

// 4. What does the pay distribution look like?
function DistributionSection({ base, depKey, currency }: SectionProps) {
  const [bins, setBins] = useState(20);
  const state = useAsync(() => api.get<Histogram>("/analytics/histogram", { ...base, bins }), [depKey, bins]);
  return (
    <Section title="Pay distribution" subtitle="How many employees fall in each band of annual pay.">
      <Field label="Number of bands">
        {(p) => (
          <select {...p} value={bins} onChange={(event) => setBins(Number(event.target.value))} style={{ maxWidth: "8rem" }}>
            {[10, 20, 30, 50].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        )}
      </Field>
      <AsyncView state={state}>
        {(data) =>
          data.bins.length === 0 ? (
            <EmptyState>No data for these filters.</EmptyState>
          ) : (
            <>
              <ChartCard
                title="Employees by annual pay"
                subtitle={`${currency}; each column is one band`}
                table={
                  <div className="table-wrap" tabIndex={0}>
                    <table>
                      <thead><tr><th scope="col" className="num">From ({currency})</th><th scope="col" className="num">To ({currency})</th><th scope="col" className="num">Employees</th></tr></thead>
                      <tbody>
                        {data.bins.map((bin, index) => (
                          <tr key={index}>
                            <td className="num">{formatAmount(bin.lower, currency)}</td>
                            <td className="num">{formatAmount(bin.upper, currency)}</td>
                            <td className="num">{formatInteger(bin.count)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                }
              >
                <HistogramChart bins={data.bins} currency={currency} />
              </ChartCard>
              <RatesNote ratesAsOf={data.rates_as_of} excluded={data.excluded_no_rate} currency={data.reporting_currency} />
            </>
          )
        }
      </AsyncView>
    </Section>
  );
}

// 5. Who is paid well below or above their peers?
const PAGE = 25;

function OutlierSection({ base, depKey, currency }: SectionProps) {
  const state = useAsync(() => api.get<Outliers>("/analytics/outliers", base), [depKey]);
  const [shown, setShown] = useState(PAGE);
  return (
    <Section title="Outliers" subtitle="Paid well below or above peers with the same job title, level and country.">
      <AsyncView state={state}>
        {(data) => (
          <>
            <p className="muted" style={{ marginTop: 0 }}>
              Flagged when pay is under {formatPercent(Number(data.low_threshold) * 100, false)} or over {formatPercent(Number(data.high_threshold) * 100, false)} of the peer median, in groups of at least {data.min_peer_group_size}. Most extreme first.
            </p>
            {data.outliers.length === 0 ? (
              <EmptyState>No outliers for these filters.</EmptyState>
            ) : (
              <>
                <div className="table-wrap" tabIndex={0}>
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">Employee</th><th scope="col">Title</th><th scope="col">Level</th><th scope="col">Country</th>
                        <th scope="col" className="num">Annual pay ({currency})</th>
                        <th scope="col" className="num">Peer median ({currency})</th>
                        <th scope="col" className="num">Peers</th>
                        <th scope="col" className="num">Pay vs median</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.outliers.slice(0, shown).map((row) => (
                        <tr key={row.employee_id}>
                          <td><Link to={`/employees/${row.employee_id}`}>{fullName(row)}</Link></td>
                          <td>{row.job_title}</td><td>{row.job_level}</td><td>{row.country}</td>
                          <td className="num">{formatAmount(row.total, currency)}</td>
                          <td className="num">{formatAmount(row.peer_median, currency)}</td>
                          <td className="num">{formatInteger(row.peer_count)}</td>
                          <td className="num">
                            <Badge tone={row.direction === "below" ? "warning" : "info"}>
                              {row.direction === "below" ? "▼ Below" : "▲ Above"} · {formatShare(row.ratio)}
                            </Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {data.outliers.length > shown && (
                  <p className="muted">
                    Showing the {formatInteger(shown)} most extreme of {formatInteger(data.outliers.length)}.{" "}
                    <button type="button" className="link" onClick={() => setShown(shown + PAGE)}>Show {PAGE} more</button>
                  </p>
                )}
              </>
            )}
            <RatesNote ratesAsOf={data.rates_as_of} excluded={data.excluded_no_rate} currency={data.reporting_currency} />
          </>
        )}
      </AsyncView>
    </Section>
  );
}

// 7. What share of pay falls into each category?
function CompositionSection({ base, depKey, currency }: SectionProps) {
  const state = useAsync(() => api.get<Composition>("/analytics/composition", base), [depKey]);
  return (
    <Section title="What pay is made of" subtitle="Share of total compensation by category. Pay outside total compensation isn't included.">
      <AsyncView state={state}>
        {(data) =>
          data.categories.length === 0 ? (
            <EmptyState>No data for these filters.</EmptyState>
          ) : (
            <>
              <ChartCard
                title="Share of total compensation"
                subtitle={`Annual, in ${currency}`}
                table={
                  <div className="table-wrap" tabIndex={0}>
                    <table>
                      <thead><tr><th scope="col">Category</th><th scope="col" className="num">Annual amount ({currency})</th><th scope="col" className="num">Share</th></tr></thead>
                      <tbody>
                        {data.categories.map((row) => (
                          <tr key={row.category}><td>{row.category}</td><td className="num">{formatAmount(row.amount, currency)}</td><td className="num">{formatShare(row.share)}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                }
              >
                <BarList
                  ariaLabel="Share of total compensation by category"
                  rows={data.categories.map((row) => ({
                    key: row.category,
                    label: row.category,
                    value: Number(row.share),
                    display: formatShare(row.share),
                    detail: `${formatMoney(row.amount, currency)} · ${formatShare(row.share)}`,
                  }))}
                />
              </ChartCard>
              <RatesNote ratesAsOf={data.rates_as_of} excluded={data.excluded_no_rate} currency={data.reporting_currency} />
            </>
          )
        }
      </AsyncView>
    </Section>
  );
}

// 6. Who had a compensation change in a period, and what was the average increase?
function ChangesSection({ base, depKey }: { base: Params; depKey: string }) {
  const yearAgo = new Date();
  yearAgo.setUTCFullYear(yearAgo.getUTCFullYear() - 1);
  const [range, setRange] = useState({ from: yearAgo.toISOString().slice(0, 10), to: todayIso() });
  const [limit, setLimit] = useState(PAGE);
  const valid = /^\d{4}-\d{2}-\d{2}$/.test(range.from) && /^\d{4}-\d{2}-\d{2}$/.test(range.to) && range.from <= range.to;
  const state = useAsync(
    () => (valid ? api.get<ChangeReport>("/analytics/changes", { ...base, date_from: range.from, date_to: range.to, limit }) : Promise.reject(new Error("invalid range"))),
    [depKey, range.from, range.to, limit],
  );
  return (
    <Section title="Pay changes" subtitle="Who had a compensation change in a period, and the average increase.">
      <div className="form-grid">
        <Field label="From" error={valid ? undefined : "Choose a valid range, with the start before the end."}>
          {(p) => <input {...p} type="date" value={range.from} max={range.to} onChange={(event) => setRange({ ...range, from: event.target.value })} />}
        </Field>
        <Field label="To">
          {(p) => <input {...p} type="date" value={range.to} min={range.from} max={todayIso()} onChange={(event) => setRange({ ...range, to: event.target.value })} />}
        </Field>
      </div>
      {valid && (
        <AsyncView state={state}>
          {(report) => (
            <>
              <div className="tiles">
                <StatTile label="Employees with a change" value={formatInteger(report.employees_changed)} />
                <StatTile label="Changes counted" value={formatInteger(report.events_counted)} note={`of ${formatInteger(report.events)} changes`} />
                <StatTile label="Average increase" value={formatPercent(report.average_increase)} note="each person's own currency" />
              </div>
              <p className="muted">
                Corrections, currency changes, new hires and changes that don't affect total compensation aren't counted in the average. Percentages compare total compensation in each person's own currency, so exchange rates play no part.
              </p>
              {report.items.length === 0 ? (
                <EmptyState>No pay changes in this period.</EmptyState>
              ) : (
                <>
                  <div className="table-wrap" tabIndex={0}>
                    <table>
                      <thead>
                        <tr>
                          <th scope="col">Effective</th><th scope="col">Employee</th><th scope="col">Reason</th>
                          <th scope="col" className="num">Total before</th><th scope="col" className="num">Total after</th>
                          <th scope="col" className="num">Change</th>
                        </tr>
                      </thead>
                      <tbody>
                        {report.items.map((item) => (
                          <tr key={`${item.employee_id}-${item.effective_date}`}>
                            <td>{formatDate(item.effective_date)}</td>
                            <td><Link to={`/employees/${item.employee_id}`}>{fullName(item)}</Link></td>
                            <td>{item.reasons.join(", ").replace(/_/g, " ")}</td>
                            <td className="num">{item.previous_total === null ? "—" : formatMoney(item.previous_total, item.currency)}</td>
                            <td className="num">{formatMoney(item.new_total, item.currency)}</td>
                            <td className="num">
                              {item.counts_as_increase ? formatPercent(item.percent_change) : <Badge>Not counted: {item.excluded_because}</Badge>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {report.events > report.items.length && (
                    <p className="muted">
                      Showing the latest {report.items.length} of {formatInteger(report.events)} changes; the counts and average cover all of them.{" "}
                      <button type="button" className="link" onClick={() => setLimit(limit + PAGE)}>Show {PAGE} more</button>
                    </p>
                  )}
                </>
              )}
            </>
          )}
        </AsyncView>
      )}
    </Section>
  );
}
