import { describe, expect, it } from "vitest";
import { QUEST_TEMPLATES, todaysQuests } from "../domain/quests";
import { XP, lockXp } from "../domain/xp";

describe("process rules", () => {
  it("pays NO TRADE at least as much as I TOOK THIS, and a journal more than that lock", () => {
    expect(lockXp("NO_TRADE")).toBeGreaterThanOrEqual(lockXp("I_TOOK_THIS"));
    expect(XP.journal).toBeGreaterThan(lockXp("I_TOOK_THIS"));
  });

  it("selects three stable process quests and never a take-N-trades quest", () => {
    const first = todaysQuests("2026-09-21", "dev");
    const second = todaysQuests("2026-09-21", "dev");
    expect(first.map((quest) => quest.id)).toEqual(second.map((quest) => quest.id));
    expect(first).toHaveLength(3);
    for (const quest of QUEST_TEMPLATES) {
      expect(quest.title.toLowerCase()).not.toMatch(/take \d+ trades/);
      expect(quest.detail.toLowerCase()).not.toMatch(/take \d+ trades/);
    }
  });
});
