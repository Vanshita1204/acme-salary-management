import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, queryString } from "./client";

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
  it("copes with an empty or non-JSON error body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>bad gateway</html>", { status: 502 })));
    await expect(api.get("/x")).rejects.toMatchObject({ status: 502, message: "Request failed (502)." });
  });
  it("returns parsed JSON on success", async () => {
    respond(200, { ok: true });
    await expect(api.get("/health")).resolves.toEqual({ ok: true });
  });
});
