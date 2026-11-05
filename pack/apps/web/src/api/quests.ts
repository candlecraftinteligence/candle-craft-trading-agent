import { useEffect, useState } from "react";
import { invalidatePackProfile } from "./profile";
import { apiFetch } from "./server";

export type ServerQuest = {
  code: string;
  title: string;
  detail: string;
  xp: number;
  period_key: string;
  progress: number;
  target: number;
  completed: boolean;
  href: string;
  cadence: "daily" | "weekly";
};

export type QuestBoard = {
  daily: ServerQuest[];
  weekly: ServerQuest[];
};

const QUEST_EVENT = "pack-quests";
const ACHIEVEMENT_EVENT = "pack-achievements";
let questCache: QuestBoard | null = null;
let achievementCache: string[] | null = null;

export function invalidateQuests(): void {
  questCache = null;
  achievementCache = null;
  window.dispatchEvent(new Event(QUEST_EVENT));
  window.dispatchEvent(new Event(ACHIEVEMENT_EVENT));
}

export function useServerQuests(): QuestBoard | null {
  const [board, setBoard] = useState<QuestBoard | null>(questCache);

  useEffect(() => {
    let cancel = false;
    const load = () => {
      if (questCache) {
        setBoard(questCache);
        return;
      }
      apiFetch("/api/quests")
        .then(async (response) => (response.ok ? response.json() : null))
        .then((next: QuestBoard | null) => {
          if (!next || cancel || !Array.isArray(next.daily)) return;
          questCache = next;
          setBoard(next);
          invalidatePackProfile();
        })
        .catch(() => undefined);
    };
    load();
    window.addEventListener(QUEST_EVENT, load);
    return () => {
      cancel = true;
      window.removeEventListener(QUEST_EVENT, load);
    };
  }, []);

  return board;
}

export function useUnlockedAchievements(): string[] | null {
  const [codes, setCodes] = useState<string[] | null>(achievementCache);

  useEffect(() => {
    let cancel = false;
    const load = () => {
      if (achievementCache) {
        setCodes(achievementCache);
        return;
      }
      apiFetch("/api/achievements")
        .then(async (response) => (response.ok ? response.json() : null))
        .then((body: { unlocked?: string[] } | null) => {
          if (!body || cancel || !Array.isArray(body.unlocked)) return;
          achievementCache = body.unlocked;
          setCodes(body.unlocked);
          invalidatePackProfile();
        })
        .catch(() => undefined);
    };
    load();
    window.addEventListener(ACHIEVEMENT_EVENT, load);
    return () => {
      cancel = true;
      window.removeEventListener(ACHIEVEMENT_EVENT, load);
    };
  }, []);

  return codes;
}
