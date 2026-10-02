import { useEffect, useState } from "react";
import type { QualityChoice, TrainingDecision } from "../domain/replayScore";
import { PACK_EVENT, readJson, writeJson } from "./records";

export type ReplayAttempt = {
  missionId: string;
  chosenTier: QualityChoice;
  chosenDecision: TrainingDecision;
  score: number;
};

const KEY = "cci-pack.replays.v1";

export function readReplays(): Record<string, ReplayAttempt> {
  const parsed = readJson<unknown>(KEY, {});
  if (!parsed || typeof parsed !== "object") return {};
  return parsed as Record<string, ReplayAttempt>;
}

export function saveReplay(attempt: ReplayAttempt): ReplayAttempt {
  const store = readReplays();
  const existing = store[attempt.missionId];
  if (existing) return existing;
  store[attempt.missionId] = attempt;
  writeJson(KEY, store);
  return attempt;
}

export function useReplays(): Record<string, ReplayAttempt> {
  const [store, setStore] = useState(readReplays);
  useEffect(() => {
    const sync = () => setStore(readReplays());
    window.addEventListener(PACK_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(PACK_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  return store;
}
