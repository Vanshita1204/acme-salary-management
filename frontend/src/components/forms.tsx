import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import { Alert } from "./ui";

interface ControlProps {
  id: string;
  "aria-invalid"?: true;
  "aria-describedby"?: string;
}

/** A labelled control with an inline hint and error, wired up for screen readers. */
export function Field({ label, error, hint, required, children }: {
  label: string;
  error?: string;
  hint?: ReactNode;
  required?: boolean;
  children: (props: ControlProps) => ReactNode;
}) {
  const id = useId();
  const describedBy = [error ? `${id}-error` : "", hint ? `${id}-hint` : ""].filter(Boolean).join(" ");
  return (
    <div className={`field${error ? " has-error" : ""}`}>
      <label htmlFor={id}>
        {label}
        {required && <span className="required" aria-hidden="true"> *</span>}
      </label>
      {children({ id, "aria-invalid": error ? true : undefined, "aria-describedby": describedBy || undefined })}
      {hint && <small id={`${id}-hint`} className="hint">{hint}</small>}
      {error && <small id={`${id}-error`} className="error-text">{error}</small>}
    </div>
  );
}

/** Submission state for a form: pending flag, server error, and field errors by name. */
export function useSubmit() {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<ApiError | undefined>();
  const run = useCallback(async (action: () => Promise<void>) => {
    setSubmitting(true);
    setError(undefined);
    try {
      await action();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, String(caught)));
    } finally {
      setSubmitting(false);
    }
  }, []);
  return { submitting, error, run, clear: () => setError(undefined) };
}

/** A server error that isn't tied to a field on screen (those show under their field). */
export function FormError({ error, shownFields = [] }: { error: ApiError | undefined; shownFields?: string[] }) {
  if (!error) return null;
  const unplaced = Object.keys(error.fields).filter((name) => !shownFields.includes(name));
  if (Object.keys(error.fields).length > 0 && unplaced.length === 0) {
    return <Alert title="Please fix the highlighted fields." />;
  }
  return <Alert>{error.message}</Alert>;
}

export function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const titleId = useId();
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const first = panel.current?.querySelector<HTMLElement>("input, select, textarea, button:not(.modal-close)");
    first?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "Tab" && panel.current) {
        const items = [...panel.current.querySelectorAll<HTMLElement>("a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)")];
        if (items.length === 0) return;
        const [head, tail] = [items[0], items[items.length - 1]];
        if (event.shiftKey && document.activeElement === head) {
          event.preventDefault();
          tail.focus();
        } else if (!event.shiftKey && document.activeElement === tail) {
          event.preventDefault();
          head.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // Clicking outside does not close: a half-filled form is not thrown away by a stray click.
  return (
    <div className="modal-backdrop">
      <div className={`modal${wide ? " modal-wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId} ref={panel}>
        <div className="modal-head">
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** Confirm before an action that is hard to undo. */
export function ConfirmModal({ title, confirmLabel, onConfirm, onClose, children, danger, busy, error }: {
  title: string;
  confirmLabel: string;
  onConfirm: () => void;
  onClose: () => void;
  children: ReactNode;
  danger?: boolean;
  busy?: boolean;
  error?: ApiError;
}) {
  return (
    <Modal title={title} onClose={onClose}>
      <div className="modal-body">
        {children}
        <FormError error={error} />
      </div>
      <div className="modal-foot">
        <button type="button" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="button" className={danger ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>
          {busy ? "Working…" : confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

export interface Option<V extends string | number = string | number> { value: V; label: string }

/** A dropdown of checkboxes: any number of choices, shown as a count on the button. */
export function MultiSelect<V extends string | number>({ label, options, selected, onChange }: {
  label: string;
  options: Option<V>[];
  selected: V[];
  onChange: (next: V[]) => void;
}) {
  const root = useRef<HTMLDetailsElement>(null);
  // Ticks show at once even if the parent applies the change a moment later (the router
  // defers URL updates), then follow the parent if it disagrees.
  const [shown, setShown] = useState(selected);
  useEffect(() => setShown(selected), [selected]);
  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (root.current?.open && !root.current.contains(event.target as Node)) root.current.open = false;
    };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, []);
  const update = (next: V[]) => {
    setShown(next);
    onChange(next);
  };
  const toggle = (value: V) =>
    update(shown.includes(value) ? shown.filter((item) => item !== value) : [...shown, value]);
  return (
    <details className="multiselect" ref={root}>
      <summary>
        {label}
        {shown.length > 0 && <span className="count">{shown.length}</span>}
      </summary>
      <div className="menu" role="group" aria-label={label}>
        {options.map((option) => (
          <label key={option.value}>
            <input type="checkbox" checked={shown.includes(option.value)} onChange={() => toggle(option.value)} />
            {option.label}
          </label>
        ))}
        {shown.length > 0 && (
          <button type="button" className="link" onClick={() => update([])}>Clear</button>
        )}
      </div>
    </details>
  );
}
