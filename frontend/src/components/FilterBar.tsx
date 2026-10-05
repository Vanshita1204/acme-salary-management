import { useEffect, useState } from "react";
import { useLoadedReference } from "../lib/context";
import { hasFilters, type View } from "../lib/view";
import { MultiSelect } from "./forms";

const STATUS_OPTIONS = [
  { value: "active", label: "Active" },
  { value: "on_leave", label: "On leave" },
  { value: "terminated", label: "Terminated" },
] as const;

/** Search and the combinable filters shared by the directory and the pay insights. */
export function FilterBar({ view, onChange, showStatus = false }: {
  view: View;
  onChange: (changes: Partial<View>) => void;
  showStatus?: boolean;
}) {
  const ref = useLoadedReference();
  const [text, setText] = useState(view.q);
  useEffect(() => setText(view.q), [view.q]);
  // Search as you type, without a request per keystroke.
  useEffect(() => {
    if (text === view.q) return;
    const timer = setTimeout(() => onChange({ q: text }), 300);
    return () => clearTimeout(timer);
  }, [text, view.q, onChange]);

  return (
    <div className="filters" role="search">
      <input
        type="search"
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="Search name, email or employee code"
        aria-label="Search by name, email or employee code"
      />
      <MultiSelect
        label="Department"
        options={ref.departments.map((d) => ({ value: d.id, label: d.name }))}
        selected={view.departmentIds}
        onChange={(departmentIds) => onChange({ departmentIds })}
      />
      <MultiSelect
        label="Country"
        options={ref.countries.map((c) => ({ value: c.code, label: `${c.name} (${c.code})` }))}
        selected={view.countries}
        onChange={(countries) => onChange({ countries })}
      />
      <MultiSelect
        label="Job title"
        options={ref.jobTitles.map((t) => ({ value: t.id, label: t.name }))}
        selected={view.titleIds}
        onChange={(titleIds) => onChange({ titleIds })}
      />
      <MultiSelect
        label="Level"
        options={ref.jobLevels.map((l) => ({ value: l.id, label: l.code }))}
        selected={view.levelIds}
        onChange={(levelIds) => onChange({ levelIds })}
      />
      {showStatus && (
        <MultiSelect
          label="Status"
          options={STATUS_OPTIONS.map((s) => ({ value: s.value, label: s.label }))}
          selected={view.statuses}
          onChange={(statuses) => onChange({ statuses })}
        />
      )}
      {hasFilters(view) && (
        <button
          type="button"
          className="link"
          onClick={() => onChange({ q: "", departmentIds: [], countries: [], titleIds: [], levelIds: [], statuses: [] })}
        >
          Clear filters
        </button>
      )}
    </div>
  );
}
