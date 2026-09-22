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

export function rankProgress(xp: number): {
  name: (typeof WOLF_RANKS)[number]["name"];
  nextName: string | null;
  nextAt: number | null;
  ratio: number;
} {
  const safeXp = Number.isFinite(xp) ? Math.max(0, xp) : 0;
  let current: (typeof WOLF_RANKS)[number] = WOLF_RANKS[0];
  for (const rank of WOLF_RANKS) {
    if (safeXp >= rank.xp) current = rank;
  }
  const index = WOLF_RANKS.findIndex((rank) => rank.name === current.name);
  const next = WOLF_RANKS[index + 1];
  if (!next) {
    return { name: current.name, nextName: null, nextAt: null, ratio: 1 };
  }
  const span = next.xp - current.xp;
  const ratio = span === 0 ? 1 : Math.min(1, (safeXp - current.xp) / span);
  return { name: current.name, nextName: next.name, nextAt: next.xp, ratio };
}
