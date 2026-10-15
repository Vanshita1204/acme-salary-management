import { describe, expect, it } from "vitest";
import { parseCacheHit, parseServerTiming } from "./timing";

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

describe("cache hit", () => {
  it("is read from the Server-Timing header", () => {
    expect(parseCacheHit('app;dur=0.4, cache;desc="hit"')).toBe(true);
    expect(parseCacheHit('app;dur=900, cache;desc="miss"')).toBe(false);
    expect(parseCacheHit(null)).toBe(false);
  });
});
