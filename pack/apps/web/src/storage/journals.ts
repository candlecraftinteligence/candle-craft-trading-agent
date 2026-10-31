import { useEffect, useState } from "react";
import { PACK_EVENT, readJson, writeJson } from "./records";

export const JOURNAL_RESULTS = ["WIN", "LOSS", "BREAKEVEN", "PARTIAL", "DID_NOT_ENTER"] as const;
export type JournalResult = (typeof JOURNAL_RESULTS)[number];

export const CONFIDENCE_LEVELS = ["LOW", "MEASURED", "HIGH"] as const;
export type ConfidenceLevel = (typeof CONFIDENCE_LEVELS)[number];

export type JournalRecord = {
  missionId: string;
  note: string;
  confidence: ConfidenceLevel;
  riskPlanNote: string;
  reason: string | null;
  result: JournalResult | null;
  lesson: string | null;
};

const KEY = "cci-pack.journals.v1";

export function readJournals(): Record<string, JournalRecord> {
  const parsed = readJson<unknown>(KEY, {});
  if (!parsed || typeof parsed !== "object") return {};
  return parsed as Record<string, JournalRecord>;
}

export function saveJournal(record: JournalRecord): JournalRecord {
  const store = readJournals();
  const existing = store[record.missionId];
  if (existing) return existing;
  store[record.missionId] = record;
  writeJson(KEY, store);
  return record;
}

export function useJournals(): Record<string, JournalRecord> {
  const [store, setStore] = useState(readJournals);
  useEffect(() => {
    const sync = () => setStore(readJournals());
    window.addEventListener(PACK_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(PACK_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  return store;
}
