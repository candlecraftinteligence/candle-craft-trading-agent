import { useEffect, useState } from "react";

export const DECISIONS = [
  { id: "TRACK", label: "TRACK" },
  { id: "I_TOOK_THIS", label: "I TOOK THIS" },
  { id: "WATCH_ONLY", label: "WATCH ONLY" },
  { id: "NO_TRADE", label: "NO TRADE" },
] as const;

export type DecisionId = (typeof DECISIONS)[number]["id"];

const STORAGE_KEY = "cci-pack.decisions.v1";
const CHANGE_EVENT = "pack-decisions";

const ALLOWED = new Set<string>(DECISIONS.map((item) => item.id));

export type DecisionStore = Record<string, DecisionId>;

function readStore(): DecisionStore {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object") return {};
    const store: DecisionStore = {};
    for (const [missionId, decision] of Object.entries(parsed)) {
      if (typeof decision === "string" && ALLOWED.has(decision)) {
        store[missionId] = decision as DecisionId;
      }
    }
    return store;
  } catch {
    return {};
  }
}

export function decisionLabel(id: DecisionId): string {
  return DECISIONS.find((item) => item.id === id)?.label ?? id;
}

export function lockDecision(missionId: string, decision: DecisionId): DecisionId {
  const store = readStore();
  const existing = store[missionId];
  if (existing) return existing;
  store[missionId] = decision;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  window.dispatchEvent(new Event(CHANGE_EVENT));
  return decision;
}

export function useDecisions(): DecisionStore {
  const [store, setStore] = useState<DecisionStore>(() => readStore());

  useEffect(() => {
    const sync = () => setStore(readStore());
    window.addEventListener(CHANGE_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(CHANGE_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  return store;
}
