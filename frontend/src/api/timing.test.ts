import { describe, expect, it } from "vitest";
import { parseServerTiming } from "./timing";

describe("Server-Timing header", () => {
  it("reads the app duration", () => {
    expect(parseServerTiming("app;dur=12.3")).toBe(12.3);
    expect(parseServerTiming("db;dur=1, app;dur=40.5")).toBe(40.5);
  });
  it("is null when the header is missing or has no app entry", () => {
    expect(parseServerTiming(null)).toBeNull();
    expect(parseServerTiming("db;dur=1")).toBeNull();
  });
});
