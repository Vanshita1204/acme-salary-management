import { describe, expect, it } from "vitest";
import { collect, parseAmount, requireDate, requireText, validEmail } from "./validation";

describe("parseAmount", () => {
  it.each([["150000", "150000"], ["1,234.5", "1234.5"], [" 99.99 ", "99.99"], ["0", "0"]])("accepts %j", (text, value) => {
    expect(parseAmount(text)).toEqual({ value });
  });
  it.each([["", /enter an amount/i], ["abc", /digits/i], ["1.234", /2 decimal/i], ["-5", /negative/i], ["1e5", /digits/i]])("rejects %j", (text, message) => {
    expect(parseAmount(text).error).toMatch(message);
  });
  it("can insist on more than zero (base pay)", () => {
    expect(parseAmount("0", { positive: true }).error).toMatch(/greater than zero/i);
    expect(parseAmount("0.01", { positive: true }).value).toBe("0.01");
  });
  it("refuses absurdly long numbers", () => {
    expect(parseAmount("1".repeat(20)).error).toMatch(/too large/i);
  });
});

describe("other checks", () => {
  it("emails", () => {
    expect(validEmail("a@b.co")).toBeUndefined();
    expect(validEmail("nope")).toMatch(/valid email/i);
    expect(validEmail("  ")).toMatch(/enter an email/i);
  });
  it("text and dates", () => {
    expect(requireText("  ", "a name")).toBe("Enter a name.");
    expect(requireText("x", "a name")).toBeUndefined();
    expect(requireDate("2026-10-05")).toBeUndefined();
    expect(requireDate("", "a hire date")).toBe("Choose a hire date.");
  });
  it("collect keeps only the problems", () => {
    expect(collect({ a: undefined, b: "bad" })).toEqual({ errors: { b: "bad" }, ok: false });
    expect(collect({ a: undefined }).ok).toBe(true);
  });
});
