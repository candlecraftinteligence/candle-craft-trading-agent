export type QuestTemplate = {
  id: "read-lock" | "no-trade" | "journal" | "replay" | "evidence";
  title: string;
  detail: string;
  href: string;
  xp: number;
};

export const QUEST_TEMPLATES: readonly QuestTemplate[] = [
  {
    id: "read-lock",
    title: "Read a mission and seal a call",
    detail: "Any of the four decisions counts. A pass counts too.",
    href: "/missions",
    xp: 40,
  },
  {
    id: "no-trade",
    title: "Pass on purpose",
    detail: "Seal NO TRADE. Passing is Pack strength.",
    href: "/missions",
    xp: 40,
  },
  {
    id: "journal",
    title: "Write the den journal",
    detail: "Your notes stay separate from the CCI outcome.",
    href: "/missions",
    xp: 40,
  },
  {
    id: "replay",
    title: "Run one closed tape",
    detail: "Train on a closed setup. The outcome stays masked until you reveal.",
    href: "/replay",
    xp: 40,
  },
  {
    id: "evidence",
    title: "Read the evidence all the way",
    detail: "Mark the evidence read on one Mission.",
    href: "/missions",
    xp: 40,
  },
];

function hash(value: string): number {
  let h = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export function todaysQuests(day: string, userId = "dev"): QuestTemplate[] {
  const pool = [...QUEST_TEMPLATES];
  const picked: QuestTemplate[] = [];
  let seed = hash(`${userId}:${day}`);
  while (picked.length < 3 && pool.length > 0) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const index = seed % pool.length;
    const [next] = pool.splice(index, 1);
    if (next) picked.push(next);
  }
  return picked;
}

export function todayKey(now = new Date()): string {
  return now.toISOString().slice(0, 10);
}
