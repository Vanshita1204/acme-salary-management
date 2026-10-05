import { render } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { AppProviders, useReference } from "../lib/context";
import { Loading } from "../components/ui";

export interface Call { method: string; path: string; query: URLSearchParams; json: unknown; form: FormData | null }
type Reply = { status?: number; body?: unknown };
export type Handler = Reply | ((call: Call) => Reply);

export const departments = [{ id: 1, name: "Engineering" }, { id: 2, name: "Sales" }];
export const titles = [{ id: 1, name: "Software Engineer" }, { id: 2, name: "Account Executive" }];
export const levels = [{ id: 1, code: "L1", label: "Level 1", rank: 1 }, { id: 2, code: "L2", label: "Level 2", rank: 2 }, { id: 3, code: "L3", label: "Level 3", rank: 3 }];
export const compensationTypes = [
  { id: 1, name: "Base Pay", category: "fixed", subtype: "base", period_months: 1, is_base_pay: true, counts_toward_total: true, created_at: "2026-01-01T00:00:00Z" },
  { id: 2, name: "Annual Bonus", category: "bonus", subtype: "annual", period_months: 12, is_base_pay: false, counts_toward_total: true, created_at: "2026-01-01T00:00:00Z" },
];
export const reasons = [{ id: 1, code: "annual_revision", label: "Annual revision" }, { id: 2, code: "market_adjustment", label: "Market adjustment" }, { id: 3, code: "correction", label: "Correction" }];

const defaults: Record<string, Handler> = {
  "GET /exchange-rates/latest": { body: [{ currency: "EUR", rate_to_usd: "1.08", rate_date: "2026-10-05" }, { currency: "INR", rate_to_usd: "0.012", rate_date: "2026-10-05" }] },
  "GET /departments": { body: departments },
  "GET /job-titles": { body: titles },
  "GET /job-levels": { body: levels },
  "GET /countries": { body: [{ code: "IN", name: "India", default_currency: "INR" }, { code: "DE", name: "Germany", default_currency: "EUR" }] },
  "GET /currencies": { body: [{ code: "INR", name: "Indian Rupee" }, { code: "EUR", name: "Euro" }, { code: "USD", name: "US Dollar" }] },
  "GET /companies": { body: [{ id: 1, name: "Globex" }] },
  "GET /compensation-types": { body: compensationTypes },
  "GET /change-reasons": { body: reasons },
};

/** Replace `fetch` with canned answers keyed "METHOD /path"; returns every call made. */
export function mockApi(handlers: Record<string, Handler> = {}) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://test");
    const path = url.pathname.replace(/^\/api/, "");
    const method = (init.method ?? "GET").toUpperCase();
    const body = init.body;
    calls.push({
      method, path, query: url.searchParams,
      json: typeof body === "string" ? JSON.parse(body) : null,
      form: body instanceof FormData ? body : null,
    });
    const handler = handlers[`${method} ${path}`] ?? defaults[`${method} ${path}`];
    if (!handler) return new Response(JSON.stringify({ detail: `no mock for ${method} ${path}` }), { status: 404 });
    const reply = typeof handler === "function" ? handler(calls[calls.length - 1]) : handler;
    return new Response(JSON.stringify(reply.body ?? null), { status: reply.status ?? 200 });
  }));
  return { calls, sent: (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path) };
}

/** Pages assume the reference lists are loaded; wait for them like the real shell does. */
function Gate({ children }: { children: ReactNode }) {
  const { ref } = useReference();
  return ref ? <>{children}</> : <Loading />;
}

export function renderApp(ui: ReactElement, route = "/") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AppProviders><Gate>{ui}</Gate></AppProviders>
    </MemoryRouter>,
  );
}
