import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, fallbackMessage, queryString, readable } from "./client";

const respond = (status: number, body: unknown) =>
  vi.stubGlobal("fetch", vi.fn(async () => new Response(body === null ? "" : JSON.stringify(body), { status })));

afterEach(() => vi.unstubAllGlobals());

describe("queryString", () => {
  it("repeats array keys and drops empties", () => {
    expect(queryString({ a: [1, 2], b: "", c: null, d: undefined, e: "x y", f: false })).toBe("?a=1&a=2&e=x+y&f=false");
    expect(queryString({})).toBe("");
  });
});

describe("errors people can read", () => {
  it("passes through the server's sentence", async () => {
    respond(409, { detail: "employee with this email already exists" });
    await expect(api.post("/employees", {})).rejects.toMatchObject({ status: 409, message: "employee with this email already exists" });
  });
  it("turns validation lists into messages by field", async () => {
    respond(422, { detail: [
      { loc: ["body", "base_pay", "amount"], msg: "Input should be greater than 0" },
      { loc: ["body", "email"], msg: "Value error, not a valid email address" },
    ] });
    const error = (await api.post("/employees", {}).catch((e) => e)) as ApiError;
    expect(error.fields).toEqual({ "base_pay.amount": "Input should be greater than 0", email: "not a valid email address" });
    expect(error.message).toContain("email: not a valid email address");
  });
  it("explains a network failure instead of leaking the browser's message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    await expect(api.get("/x")).rejects.toMatchObject({ status: 0, message: expect.stringMatching(/can't reach the server/i) });
  });
  it("keeps a structured body, such as an import's report", async () => {
    respond(422, { ok: false, errors: [{ row: 3, column: "email", reason: "bad" }] });
    const error = (await api.postForm("/import/confirm", new FormData()).catch((e) => e)) as ApiError;
    expect(error.status).toBe(422);
    expect(error.body).toMatchObject({ ok: false, errors: [{ row: 3 }] });
  });
  it("copes with an empty or non-JSON error body, with a sentence that fits the failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>bad gateway</html>", { status: 502 })));
    await expect(api.get("/x")).rejects.toMatchObject({ status: 502, message: expect.stringMatching(/isn't available right now/) });
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Internal Server Error", { status: 500 })));
    await expect(api.get("/x")).rejects.toMatchObject({ message: expect.stringMatching(/went wrong on the server/) });
  });
  it("names the path parameter problem without a confusing prefix", async () => {
    respond(422, { detail: [{ loc: ["path", "employee_id"], msg: "Input should be a valid integer" }] });
    const error = (await api.get("/employees/abc").catch((e) => e)) as ApiError;
    expect(error.fields).toEqual({ employee_id: "Input should be a valid integer" });
  });
  it("returns parsed JSON on success", async () => {
    respond(200, { ok: true });
    await expect(api.get("/health")).resolves.toEqual({ ok: true });
  });
});

describe("readable", () => {
  it("turns field names and ISO dates in a server sentence into plain words", () => {
    expect(readable("effective_date can't be before the current Base Pay record (2025-06-01); it would never become current"))
      .toBe("effective date can't be before the current Base Pay record (1 Jun 2025); it would never become current");
    expect(readable("effective_date cannot precede hire_date")).toBe("effective date cannot precede hire date");
  });
  it("leaves ordinary sentences alone", () => {
    expect(readable("employee with this email already exists")).toBe("employee with this email already exists");
    expect(readable("unsupported currency: ZZZ")).toBe("unsupported currency: ZZZ");
  });
  it("is what people see for a refusal", async () => {
    respond(422, { detail: "date_from must not be after date_to" });
    await expect(api.get("/x")).rejects.toMatchObject({ message: "date from must not be after date to" });
  });
});

describe("fallbackMessage", () => {
  it.each([
    [0, /./], [403, /permission/], [404, /wasn't found/], [413, /too large/], [429, /Too many/], [503, /isn't available/], [500, /went wrong/], [418, /error 418/],
  ])("%i gets a plain sentence", (status, pattern) => {
    expect(fallbackMessage(status)).toMatch(pattern);
    expect(fallbackMessage(status)).not.toMatch(/Request failed|undefined|\[object/);
  });
});
