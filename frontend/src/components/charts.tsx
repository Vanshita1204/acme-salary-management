import type { ReactNode } from "react";
import { formatCompact, formatInteger, formatMoney } from "../lib/format";
import type { Bin } from "../api/types";

/** A chart with its title and a table of the same numbers (no chart is color-only or hover-only). */
export function ChartCard({ title, subtitle, children, table }: { title: string; subtitle?: ReactNode; children: ReactNode; table: ReactNode }) {
  return (
    <section className="card chart-card">
      <h3>{title}</h3>
      {subtitle && <p className="muted chart-subtitle">{subtitle}</p>}
      {children}
      <details className="table-view">
        <summary>View as table</summary>
        {table}
      </details>
    </section>
  );
}

export interface BarRow { key: string; label: string; value: number; display: string; detail: string }

/**
 * Horizontal bars for comparing magnitude across named groups: one hue, value at the
 * tip, the full detail in the tooltip. Names are on the left so long ones fit.
 */
export function BarList({ rows, ariaLabel }: { rows: BarRow[]; ariaLabel: string }) {
  const max = Math.max(...rows.map((row) => row.value), 0);
  return (
    <ul className="bar-list" aria-label={ariaLabel}>
      {rows.map((row) => (
        <li key={row.key} title={`${row.label}: ${row.detail}`}>
          <span className="bar-label">{row.label}</span>
          <span className="bar-track">
            <span className="bar" style={{ width: `${max > 0 ? Math.max((row.value / max) * 100, 0.5) : 0}%` }} />
          </span>
          <span className="bar-value">{row.display}</span>
        </li>
      ))}
    </ul>
  );
}

/** Pay distribution: equal-width columns on a zero baseline, with the count on hover and in the table. */
export function HistogramChart({ bins, currency }: { bins: Bin[]; currency: string }) {
  const max = Math.max(...bins.map((bin) => bin.count), 0);
  const first = bins[0];
  const last = bins[bins.length - 1];
  return (
    <div className="histogram" role="img" aria-label={`Pay distribution in ${bins.length} bins from ${formatMoney(first?.lower, currency)} to ${formatMoney(last?.upper, currency)}`}>
      <div className="histogram-y muted" aria-hidden="true">
        <span>{formatInteger(max)}</span>
        <span>0</span>
      </div>
      <div className="histogram-plot">
        <div className="histogram-bars">
          {bins.map((bin, index) => (
            <div
              key={index}
              className="histogram-slot"
              title={`${formatMoney(bin.lower, currency)} to ${formatMoney(bin.upper, currency)}: ${formatInteger(bin.count)} employee${bin.count === 1 ? "" : "s"}`}
            >
              <div className="column" style={{ height: `${max > 0 ? (bin.count / max) * 100 : 0}%` }} />
            </div>
          ))}
        </div>
        <div className="histogram-x muted" aria-hidden="true">
          <span>{formatCompact(first?.lower, currency)}</span>
          <span>{formatCompact(last?.upper, currency)}</span>
        </div>
      </div>
    </div>
  );
}
