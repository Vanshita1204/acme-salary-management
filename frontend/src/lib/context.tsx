import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { ChangeReason, Company, CompensationType, Country, Currency, Department, JobLevel, JobTitle, RateRow } from "../api/types";
import { readStored, writeStored } from "./storage";
import { useAsync } from "./useAsync";

// --- who is making changes (there is no login; `changed_by` is a label) ---

interface ActorValue { actor: string; setActor: (name: string) => void }
const ActorContext = createContext<ActorValue>({ actor: "", setActor: () => {} });
export const useActor = () => useContext(ActorContext);

// --- the reporting currency, chosen once and applied everywhere (FR-8) ---

interface ReportingValue {
  currency: string;
  setCurrency: (code: string) => void;
  /** Currencies with a stored rate (USD is always the base). */
  available: string[];
}
const ReportingContext = createContext<ReportingValue>({ currency: "USD", setCurrency: () => {}, available: ["USD"] });
export const useReporting = () => useContext(ReportingContext);

// --- reference lists used by filters and forms ---

export interface Reference {
  departments: Department[];
  jobTitles: JobTitle[];
  jobLevels: JobLevel[];
  countries: Country[];
  currencies: Currency[];
  companies: Company[];
  compensationTypes: CompensationType[];
  changeReasons: ChangeReason[];
}
interface ReferenceValue {
  ref: Reference | undefined;
  loading: boolean;
  error: string | undefined;
  /** Re-read the lists after one was edited. */
  refresh: () => void;
}
const ReferenceContext = createContext<ReferenceValue>({ ref: undefined, loading: true, error: undefined, refresh: () => {} });
export const useReference = () => useContext(ReferenceContext);

/** The loaded lists; only call under screens that render once they're ready. */
export function useLoadedReference(): Reference {
  const { ref } = useReference();
  if (!ref) throw new Error("reference data isn't loaded yet");
  return ref;
}

const ACTOR_KEY = "acme.actor";
const REPORTING_KEY = "acme.reportingCurrency";

export function AppProviders({ children }: { children: ReactNode }) {
  const [actor, setActorState] = useState(() => readStored(ACTOR_KEY) ?? "");
  const setActor = useCallback((name: string) => {
    setActorState(name);
    writeStored(ACTOR_KEY, name);
  }, []);

  const rates = useAsync(() => api.get<RateRow[]>("/exchange-rates/latest"), []);
  const available = useMemo(() => {
    const codes = new Set(["USD", ...(rates.data ?? []).map((rate) => rate.currency)]);
    return [...codes].sort();
  }, [rates.data]);
  const [currency, setCurrencyState] = useState(() => readStored(REPORTING_KEY) ?? "USD");
  const setCurrency = useCallback((code: string) => {
    setCurrencyState(code);
    writeStored(REPORTING_KEY, code);
  }, []);
  // A saved choice that no longer has a rate falls back to USD instead of showing blanks.
  useEffect(() => {
    if (rates.data && !available.includes(currency)) setCurrencyState("USD");
  }, [rates.data, available, currency]);

  const reference = useAsync(async (): Promise<Reference> => {
    const [departments, jobTitles, jobLevels, countries, currencies, companies, compensationTypes, changeReasons] = await Promise.all([
      api.get<Department[]>("/departments"),
      api.get<JobTitle[]>("/job-titles"),
      api.get<JobLevel[]>("/job-levels"),
      api.get<Country[]>("/countries"),
      api.get<Currency[]>("/currencies"),
      api.get<Company[]>("/companies"),
      api.get<CompensationType[]>("/compensation-types"),
      api.get<ChangeReason[]>("/change-reasons"),
    ]);
    return { departments, jobTitles, jobLevels, countries, currencies, companies, compensationTypes, changeReasons };
  }, []);

  const actorValue = useMemo(() => ({ actor, setActor }), [actor, setActor]);
  const reportingValue = useMemo(() => ({ currency, setCurrency, available }), [currency, setCurrency, available]);
  const referenceValue = useMemo(
    () => ({ ref: reference.data, loading: reference.loading, error: reference.error?.message, refresh: reference.reload }),
    [reference.data, reference.loading, reference.error, reference.reload],
  );

  return (
    <ActorContext.Provider value={actorValue}>
      <ReportingContext.Provider value={reportingValue}>
        <ReferenceContext.Provider value={referenceValue}>{children}</ReferenceContext.Provider>
      </ReportingContext.Provider>
    </ActorContext.Provider>
  );
}
