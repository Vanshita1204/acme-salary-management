import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api/client";
import { describeRates } from "../lib/format";
import type { AsyncState } from "../lib/useAsync";

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {actions && <div className="actions">{actions}</div>}
    </header>
  );
}

export function Alert({ kind = "error", title, children }: { kind?: "error" | "success" | "info" | "warning"; title?: string; children?: ReactNode }) {
  const icon = { error: "✕", success: "✓", info: "i", warning: "!" }[kind];
  return (
    <div className={`alert alert-${kind}`} role={kind === "error" ? "alert" : "status"}>
      <span className="alert-icon" aria-hidden="true">{icon}</span>
      <div>
        {title && <strong>{title}</strong>}
        {children && <div>{children}</div>}
      </div>
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <span className="spinner" aria-hidden="true" /> {label}
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  // Retrying a "not found" can't help; point back to somewhere that works instead.
  if (error.status === 404) {
    return (
      <Alert kind="warning" title="Not found">
        {error.message} <Link to="/">Back to all employees</Link>
      </Alert>
    );
  }
  return (
    <Alert title={error.status === 0 || error.status >= 500 ? "Can't load this right now" : "Something went wrong"}>
      {error.message}
      {onRetry && (
        <>
          {" "}
          <button type="button" className="link" onClick={onRetry}>Try again</button>
        </>
      )}
    </Alert>
  );
}

/**
 * Loading, error or content for one request. Old content stays visible (dimmed) while the
 * next is loading, and if the next load fails it says so above the old content instead of
 * letting figures that no longer match the filters pass as current.
 */
export function AsyncView<T>({ state, children, label }: { state: AsyncState<T>; children: (data: T) => ReactNode; label?: string }) {
  if (state.data !== undefined) {
    return (
      <>
        {state.error && <StaleNotice error={state.error} onRetry={state.reload} />}
        <div className={state.loading || state.error ? "reloading" : undefined} aria-busy={state.loading}>
          {children(state.data)}
        </div>
      </>
    );
  }
  if (state.error) return <ErrorBox error={state.error} onRetry={state.reload} />;
  return <Loading label={label} />;
}

function StaleNotice({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  return (
    <Alert title="Couldn't update this">
      {error.message} What's shown below is from before and may not match your latest choices.{" "}
      <button type="button" className="link" onClick={onRetry}>Try again</button>
    </Alert>
  );
}

export function Badge({ tone = "neutral", children }: { tone?: "neutral" | "good" | "warning" | "critical" | "info"; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function StatTile({ label, value, note }: { label: string; value: ReactNode; note?: ReactNode }) {
  return (
    <div className="stat-tile">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {note && <div className="stat-note muted">{note}</div>}
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Which exchange rates a figure used (FR-8) and who was left out for lack of one. */
export function RatesNote({ ratesAsOf, excluded = 0, currency }: { ratesAsOf: Record<string, string>; excluded?: number; currency?: string }) {
  return (
    <p className="rates-note muted">
      {currency && <>All amounts in {currency}. </>}
      {/* "No rates were needed" would contradict "left out for lack of a rate". */}
      {Object.keys(ratesAsOf).length === 0 && excluded > 0 ? "" : describeRates(ratesAsOf)}
      {excluded > 0 && (
        <>
          {" "}
          <strong>
            {excluded} employee{excluded === 1 ? " is" : "s are"} left out: no exchange rate is stored for their currency.
          </strong>
        </>
      )}
    </p>
  );
}
