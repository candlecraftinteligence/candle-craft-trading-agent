export const WOLF_RANKS = [
  { name: "SCOUT", xp: 0 },
  { name: "TRACKER", xp: 250 },
  { name: "HUNTER", xp: 750 },
  { name: "PATHFINDER", xp: 1800 },
  { name: "VANGUARD", xp: 4000 },
  { name: "ELITE", xp: 8500 },
] as const;

export const PLACEHOLDER_RANK = "SCOUT";
export const PLACEHOLDER_XP = 0;
export const NEXT_RANK = WOLF_RANKS[1];
