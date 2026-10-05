// The only place that talks to the backend. Every failure becomes an ApiError whose
// message is safe to show a person; field-level problems are kept for inline display.

export const API_URL: string = import.meta.env.VITE_API_URL ?? "/api";

export type Params = Record<string, string | number | boolean | null | undefined | (string | number)[]>;

/** Query string from params: arrays repeat the key, empty values are dropped. */
export function queryString(params: Params = {}): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item === null || item === undefined || item === "") continue;
      search.append(key, String(item));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export class ApiError extends Error {
  readonly status: number;
  /** Problems by request field name (from FastAPI validation errors). */
  readonly fields: Record<string, string>;
  /** The body, for responses with structure (e.g. an import's validation report). */
  readonly body: unknown;

  constructor(status: number, message: string, fields: Record<string, string> = {}, body: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fields = fields;
    this.body = body;
  }
}

interface ValidationIssue { loc?: (string | number)[]; msg?: string }

/** FastAPI validation errors: detail is a list of {loc, msg}. */
function fromDetail(status: number, detail: unknown, body: unknown): ApiError {
  if (typeof detail === "string") return new ApiError(status, detail, {}, body);
  if (Array.isArray(detail)) {
    const fields: Record<string, string> = {};
    const lines: string[] = [];
    for (const issue of detail as ValidationIssue[]) {
      const path = (issue.loc ?? []).filter((part) => part !== "body" && part !== "query");
      const field = path.map(String).join(".");
      const msg = (issue.msg ?? "invalid value").replace(/^Value error, /, "");
      if (field && !(field in fields)) fields[field] = msg;
      lines.push(field ? `${field}: ${msg}` : msg);
    }
    return new ApiError(status, lines.join("; ") || "The request was not valid.", fields, body);
  }
  return new ApiError(status, `Request failed (${status}).`, {}, body);
}

async function request<T>(method: string, path: string, init: { params?: Params; json?: unknown; form?: FormData } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  let body: BodyInit | undefined;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  } else if (init.form) {
    body = init.form;
  }
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}${queryString(init.params)}`, { method, headers, body });
  } catch {
    throw new ApiError(0, "Can't reach the server. Check your connection and try again.");
  }
  const text = await response.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }
  if (!response.ok) {
    const detail = (parsed as { detail?: unknown } | null)?.detail;
    throw fromDetail(response.status, detail, parsed);
  }
  return parsed as T;
}

export const api = {
  get: <T>(path: string, params?: Params) => request<T>("GET", path, { params }),
  post: <T>(path: string, json?: unknown) => request<T>("POST", path, { json }),
  patch: <T>(path: string, json: unknown) => request<T>("PATCH", path, { json }),
  postForm: <T>(path: string, form: FormData) => request<T>("POST", path, { form }),
};
