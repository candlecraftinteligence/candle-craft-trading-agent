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
  { id: "A09", name: "Quiet Week", category: "Restraint", rarity: "Epic", rule: "Full week with a process action each day and no I TOOK THIS" },
  { id: "A10", name: "Streak Seven", category: "Belonging", rarity: "Rare", rule: "Discipline Streak 7" },
  { id: "A11", name: "Streak Thirty", category: "Belonging", rarity: "Epic", rule: "Discipline Streak 30" },
  { id: "A12", name: "Pathwalker", category: "Process", rarity: "Rare", rule: "Reach PATHFINDER" },
  { id: "A13", name: "Vanguard Seal", category: "Belonging", rarity: "Epic", rule: "Reach VANGUARD" },
  { id: "A14", name: "Elite Howl", category: "Belonging", rarity: "Legendary", rule: "Reach ELITE" },
  { id: "A15", name: "Outcome Separatist", category: "Review", rarity: "Rare", rule: "Journal plus CCI outcome on 5 Missions" },
  { id: "A16", name: "No Chase", category: "Restraint", rarity: "Epic", rule: "After an INVALIDATED Mission, pass or watch" },
  { id: "A17", name: "Vault Dweller", category: "Replay", rarity: "Rare", rule: "25 Replay completions" },
  { id: "A18", name: "Pack Oath", category: "Belonging", rarity: "Common", rule: "Onboarding plus first lock" },
];
