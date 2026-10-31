export const TRAINING_DECISIONS = [
  { id: "TRACK", label: "TRACK" },
  { id: "TAKE", label: "TAKE" },
  { id: "WATCH", label: "WATCH" },
  { id: "NO_TRADE", label: "NO TRADE" },
] as const;

export type TrainingDecision = (typeof TRAINING_DECISIONS)[number]["id"];
export type QualityChoice = "HUNT" | "STANDARD";

export type ReplayScore = {
  quality: number;
  decision: number;
  attention: number;
  total: number;
};

export function scoreReplay(input: {
  actualTier: string;
  chosenTier: QualityChoice;
  preferred: TrainingDecision;
  chosen: TrainingDecision;
  evidenceReviewed: boolean;
}): ReplayScore {
  const quality = input.actualTier === input.chosenTier ? 40 : 0;
  const decision = input.preferred === input.chosen ? 40 : 0;
  const attention = input.evidenceReviewed ? 20 : 0;
  return { quality, decision, attention, total: quality + decision + attention };
}
