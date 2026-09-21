export type AchievementCard = {
  id: string;
  name: string;
  category: string;
  rarity: string;
  rule: string;
};

export const ACHIEVEMENTS: readonly AchievementCard[] = [
  { id: "A01", name: "First Lock", category: "Process", rarity: "Common", rule: "Lock any decision once" },
  { id: "A02", name: "Evidence Reader", category: "Process", rarity: "Common", rule: "Full evidence read ×5" },
  { id: "A03", name: "Journal Ink", category: "Review", rarity: "Common", rule: "3 journals" },
  { id: "A04", name: "Clean Pass", category: "Restraint", rarity: "Rare", rule: "NO TRADE ×10" },
  { id: "A05", name: "Patient Scout", category: "Restraint", rarity: "Rare", rule: "NO TRADE on a HUNT-tier Mission" },
  { id: "A06", name: "Review Ritual", category: "Review", rarity: "Rare", rule: "Review checklist ×10" },
  { id: "A07", name: "Replay Initiate", category: "Replay", rarity: "Common", rule: "Complete 5 Replays" },
  { id: "A08", name: "Pattern Eye", category: "Replay", rarity: "Rare", rule: "≥80% score ×5 Replays" },
  { id: "A10", name: "Streak Seven", category: "Belonging", rarity: "Rare", rule: "Discipline Streak 7" },
  { id: "A15", name: "Outcome Separatist", category: "Review", rarity: "Rare", rule: "Journal plus CCI outcome on 5 Missions" },
  { id: "A17", name: "Vault Dweller", category: "Replay", rarity: "Rare", rule: "25 Replay completions" },
  { id: "A18", name: "Pack Oath", category: "Belonging", rarity: "Common", rule: "Onboarding plus first lock" },
];

export type AchievementStats = {
  locks: number;
  evidenceReads: number;
  journals: number;
  noTrade: number;
  noTradeOnHunt: number;
  reviews: number;
  replays: number;
  highScores: number;
  outcomeJournals: number;
};

export function unlockedAchievementIds(stats: AchievementStats): Set<string> {
  const unlocked = new Set<string>();
  if (stats.locks >= 1) unlocked.add("A01");
  if (stats.evidenceReads >= 5) unlocked.add("A02");
  if (stats.journals >= 3) unlocked.add("A03");
  if (stats.noTrade >= 10) unlocked.add("A04");
  if (stats.noTradeOnHunt >= 1) unlocked.add("A05");
  if (stats.reviews >= 10) unlocked.add("A06");
  if (stats.replays >= 5) unlocked.add("A07");
  if (stats.highScores >= 5) unlocked.add("A08");
  if (stats.outcomeJournals >= 5) unlocked.add("A15");
  if (stats.replays >= 25) unlocked.add("A17");
  return unlocked;
}
