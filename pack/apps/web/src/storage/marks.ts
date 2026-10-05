import { useEffect, useState } from "react";
import { PACK_EVENT, readJson, writeJson } from "./records";

type MarkStore = {
  evidenceRead: string[];
  reviews: string[];
};

const KEY = "cci-pack.marks.v1";
const EMPTY: MarkStore = { evidenceRead: [], reviews: [] };

function readMarks(): MarkStore {
  const parsed = readJson<Partial<MarkStore>>(KEY, EMPTY);
  return {
    evidenceRead: Array.isArray(parsed.evidenceRead) ? parsed.evidenceRead.filter((id) => typeof id === "string") : [],
    reviews: Array.isArray(parsed.reviews) ? parsed.reviews.filter((id) => typeof id === "string") : [],
  };
}

function add(list: string[], id: string): string[] {
  return list.includes(id) ? list : [...list, id];
}

export function markEvidenceRead(missionId: string): void {
  const marks = readMarks();
  writeJson(KEY, { ...marks, evidenceRead: add(marks.evidenceRead, missionId) });
}

export function markReview(missionId: string): void {
  const marks = readMarks();
  writeJson(KEY, { ...marks, reviews: add(marks.reviews, missionId) });
}

export function useMarks(): MarkStore {
  const [store, setStore] = useState(readMarks);
  useEffect(() => {
    const sync = () => setStore(readMarks());
    window.addEventListener(PACK_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(PACK_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  return store;
}
