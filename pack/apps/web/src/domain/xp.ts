import type { DecisionId } from "../decisions/localDecisions";

export const XP = {
  evidenceRead: 5,
  noTrade: 25,
  track: 15,
  watchOnly: 15,
  tookThis: 20,
  journal: 30,
  review: 20,
  replayComplete: 15,
  replayHigh: 25,
  quest: 40,
} as const;

export function lockXp(decision: DecisionId): number {
  switch (decision) {
    case "NO_TRADE":
      return XP.noTrade;
    case "I_TOOK_THIS":
      return XP.tookThis;
    case "TRACK":
      return XP.track;
    case "WATCH_ONLY":
      return XP.watchOnly;
  }
}

export type XpLine = {
  id: string;
  label: string;
  amount: number;
};

export function missionPreview(input: {
  evidenceRead: boolean;
  decision: DecisionId | null;
  journalSaved: boolean;
  resolved: boolean;
  reviewSaved: boolean;
}): { lines: XpLine[]; total: number } {
  const lines: XpLine[] = [];
  if (input.evidenceRead) {
    lines.push({ id: "evidence", label: "Evidence read", amount: XP.evidenceRead });
  }
  if (input.decision) {
    lines.push({ id: "lock", label: "Decision lock", amount: lockXp(input.decision) });
  }
  if (input.journalSaved && input.resolved) {
    lines.push({ id: "journal", label: "Journal after resolution", amount: XP.journal });
  }
  if (input.reviewSaved) {
    lines.push({ id: "review", label: "Outcome review", amount: XP.review });
  }
  return { lines, total: lines.reduce((sum, line) => sum + line.amount, 0) };
}

export function replayPreview(score: number): { lines: XpLine[]; total: number } {
  const lines: XpLine[] = [{ id: "replay", label: "Replay completed", amount: XP.replayComplete }];
  if (score >= 80) {
    lines.push({ id: "replay-high", label: "Replay score at least 80", amount: XP.replayHigh });
  }
  return { lines, total: lines.reduce((sum, line) => sum + line.amount, 0) };
}
