// Shapes of the backend's JSON. Money and rates arrive as decimal strings so no
// precision is lost; they are parsed only for display (see lib/format.ts).

export type Decimal = string;
export type Status = "active" | "on_leave" | "terminated";

export interface Currency { code: string; name: string }
export interface Country { code: string; name: string; default_currency: string }
export interface Company { id: number; name: string }
export interface Department { id: number; name: string }
export interface JobTitle { id: number; name: string }
export interface JobLevel { id: number; code: string; label: string; rank: number }
export interface ChangeReason { id: number; code: string; label: string }

export interface CompensationType {
  id: number;
  name: string;
  category: string;
  subtype: string | null;
  period_months: number;
  is_base_pay: boolean;
  counts_toward_total: boolean;
  created_at: string;
}

export interface Employee {
  id: number;
  code: string;
  company_id: number;
  first_name: string;
  last_name: string;
  email: string;
  department_id: number;
  department: string;
  job_title_id: number;
  job_title: string;
  job_level_id: number;
  job_level: string;
  current_country: string;
  currency: string;
  status: Status;
  hire_date: string;
  termination_date: string | null;
}

export interface DirectoryItem {
  id: number;
  code: string;
  first_name: string;
  last_name: string;
  email: string;
  department: string;
  job_title: string;
  job_level: string;
  current_country: string;
  status: Status;
  hire_date: string;
  currency: string;
  total_compensation: Decimal | null;
  total_compensation_reporting: Decimal | null;
}

export interface DirectoryPage {
  items: DirectoryItem[];
  next_cursor: string | null;
  prev_cursor: string | null;
  reporting_currency: string;
  rates_as_of: Record<string, string>;
}

export interface CurrentItem {
  compensation_type_id: number;
  name: string;
  category: string;
  subtype: string | null;
  period_months: number;
  is_base_pay: boolean;
  counts_toward_total: boolean;
  record_id: number;
  effective_date: string;
  amount: Decimal;
  annual_amount: Decimal;
  annual_amount_reporting: Decimal | null;
}

export interface HistoryItem {
  record_id: number;
  compensation_type_id: number;
  compensation_type: string;
  effective_date: string;
  country: string;
  currency: string;
  amount: Decimal;
  previous_amount: Decimal | null;
  percent_change: Decimal | null;
  currency_changed: boolean;
  change_reason: string;
  change_reason_label: string;
  note: string | null;
  changed_by: string;
  created_at: string;
}

export interface Profile {
  employee: Employee;
  reporting_currency: string;
  rates_as_of: Record<string, string>;
  total_compensation: Decimal;
  total_compensation_reporting: Decimal | null;
  current: CurrentItem[];
  history: HistoryItem[];
}

export interface CompensationChange {
  id: number;
  employee_id: number;
  compensation_type_id: number;
  change_reason_id: number;
  effective_date: string;
  country: string;
  currency: string;
  amount: Decimal;
  note: string | null;
  changed_by: string;
  created_at: string;
  is_current: boolean;
}

export interface ChangeResult { employee: Employee; records: CompensationChange[] }

export interface RowError { row: number; column: string; reason: string }
export interface ImportPreviewRow {
  row: number;
  first_name: string;
  last_name: string;
  email: string;
  company_id: number;
  department_id: number;
  job_title_id: number;
  job_level_id: number;
  country: string;
  hire_date: string;
  currency: string;
  base_pay_amount: Decimal;
}
export interface ValidationReport {
  ok: boolean;
  row_count: number;
  errors: RowError[];
  preview: ImportPreviewRow[];
}
export interface ImportConfirmed { created: number; employee_ids: number[] }

export interface RateRow { currency: string; rate_to_usd: Decimal; rate_date: string }

// --- analytics ---

export interface AnalyticsContext {
  reporting_currency: string;
  rates_as_of: Record<string, string>;
  excluded_no_rate: number;
}
export interface CostRow { key: string; label: string; headcount: number; total_cost: Decimal; share: Decimal }
export interface Summary extends AnalyticsContext {
  headcount: number;
  total_cost: Decimal | null;
  average: Decimal | null;
  median: Decimal | null;
  by_country: CostRow[];
  by_department: CostRow[];
}
export interface StatsRow {
  key: string;
  label: string;
  headcount: number;
  average: Decimal;
  median: Decimal;
  minimum: Decimal;
  maximum: Decimal;
}
export type GroupBy = "department" | "country" | "title" | "level";
export interface Stats extends AnalyticsContext { group_by: GroupBy; groups: StatsRow[] }
export interface RoleCountryRow {
  job_title_id: number;
  job_title: string;
  job_level_id: number;
  job_level: string;
  country: string;
  headcount: number;
  average: Decimal;
  median: Decimal;
}
export interface RoleByCountry extends AnalyticsContext { rows: RoleCountryRow[] }
export interface Bin { lower: Decimal; upper: Decimal; count: number }
export interface Histogram extends AnalyticsContext { bins: Bin[] }
export interface OutlierRow {
  employee_id: number;
  code: string;
  first_name: string;
  last_name: string;
  job_title: string;
  job_level: string;
  country: string;
  total: Decimal;
  peer_median: Decimal;
  peer_count: number;
  ratio: Decimal;
  direction: "below" | "above";
}
export interface Outliers extends AnalyticsContext {
  low_threshold: Decimal;
  high_threshold: Decimal;
  min_peer_group_size: number;
  outliers: OutlierRow[];
}
export interface CompositionRow { category: string; amount: Decimal; share: Decimal }
export interface Composition extends AnalyticsContext { categories: CompositionRow[] }
export interface ChangeEvent {
  employee_id: number;
  code: string;
  first_name: string;
  last_name: string;
  effective_date: string;
  reasons: string[];
  currency: string;
  previous_total: Decimal | null;
  new_total: Decimal;
  percent_change: Decimal | null;
  counts_as_increase: boolean;
  excluded_because: string | null;
}
export interface ChangeReport {
  date_from: string;
  date_to: string;
  employees_changed: number;
  events: number;
  events_counted: number;
  average_increase: Decimal | null;
  items: ChangeEvent[];
}
