import { useSyncExternalStore } from "react";

// How long each request took, as the server saw it (the `Server-Timing` header: request
// received to response ready) and as the browser saw it (the full round trip). The gap is
// the network plus, on a hosted free tier, any wait for the service to wake up.

export interface Timing { path: string; serverMs: number | null; totalMs: number; cached: boolean }

const latest = new Map<string, Timing>();
let snapshot: Timing[] = [];
const listeners = new Set<() => void>();

/** Milliseconds from a `Server-Timing: app;dur=12.3` header, or null if absent. */
export function parseServerTiming(header: string | null): number | null {
  const match = header?.match(/(?:^|,)\s*app;(?:[^,]*;)?dur=([\d.]+)/);
  return match ? Number(match[1]) : null;
}

/** True when the server answered from its response cache (`cache;desc="hit"`). */
export function parseCacheHit(header: string | null): boolean {
  return /cache;desc="?hit"?/.test(header ?? "");
}

export function recordTiming(path: string, serverMs: number | null, totalMs: number, cached = false) {
  latest.set(path, { path, serverMs, totalMs, cached });
  snapshot = [...latest.values()];
  listeners.forEach((listener) => listener());
}

/** The most recent timing of every endpoint called whose path starts with `prefix`. */
export function useTimings(prefix: string): Timing[] {
  const all = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
  );
  return all.filter((timing) => timing.path.startsWith(prefix)).sort((a, b) => a.path.localeCompare(b.path));
}
