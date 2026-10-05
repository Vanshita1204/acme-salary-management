import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";

export interface AsyncState<T> {
  data?: T;
  error?: ApiError;
  loading: boolean;
  reload: () => void;
}

interface Settled<T> {
  data?: T;
  error?: ApiError;
  /** The inputs these results were fetched for. */
  deps: readonly unknown[];
  tick: number;
  done: boolean;
}

const same = (a: readonly unknown[], b: readonly unknown[]) => a.length === b.length && a.every((value, index) => Object.is(value, b[index]));

/**
 * Runs `load` on mount and whenever `deps` change; a stale response never overwrites a newer one.
 * `loading` is true from the very render in which the inputs change, so results for old inputs are
 * never shown as if they were current. Old data stays available while the new data loads.
 */
export function useAsync<T>(load: () => Promise<T>, deps: readonly unknown[]): AsyncState<T> {
  const [settled, setSettled] = useState<Settled<T>>({ deps, tick: 0, done: false });
  const [tick, setTick] = useState(0);
  const latest = useRef(0);

  useEffect(() => {
    const id = ++latest.current;
    load().then(
      (data) => {
        if (id === latest.current) setSettled({ data, deps, tick, done: true });
      },
      (error: unknown) => {
        if (id !== latest.current) return;
        const apiError = error instanceof ApiError ? error : new ApiError(0, String(error));
        setSettled((previous) => ({ data: previous.data, error: apiError, deps, tick, done: true }));
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);
  const current = settled.done && same(settled.deps, deps) && settled.tick === tick;
  return {
    data: settled.data,
    // A failure belongs to the inputs that caused it; once they change or it reloads, don't show it.
    error: current ? settled.error : undefined,
    loading: !current,
    reload,
  };
}
