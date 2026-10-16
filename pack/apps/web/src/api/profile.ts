import { useEffect, useState } from "react";
import type { DecisionId } from "../decisions/localDecisions";
import { rememberDecision } from "../decisions/localDecisions";
import { apiFetch } from "./server";

export type PackProfile = {
  display_name: string;
  telegram_user_id: number;
  pack_xp: number;
  wolf_rank: string;
  discipline_streak: number;
  decisions: Record<string, DecisionId>;
  journal_ids: string[];
  replay_count: number;
  no_trade_count: number;
  notification_prefs?: NotificationPrefs;
  oath_accepted?: boolean;
};

export type NotificationPrefs = {
  new_mission: boolean;
  lifecycle_resolution: boolean;
  quest_complete: boolean;
  streak: boolean;
  replay_nudge: boolean;
};

const EVENT = "pack-profile";
let cache: PackProfile | null = null;

export function invalidatePackProfile(): void {
  cache = null;
  window.dispatchEvent(new Event(EVENT));
}

export function usePackProfile(): PackProfile | null {
  const [profile, setProfile] = useState<PackProfile | null>(cache);

  useEffect(() => {
    let cancel = false;
    const load = () => {
      if (cache) {
        setProfile(cache);
        return;
      }
      apiFetch("/api/me")
        .then(async (response) => {
          if (!response.ok || cancel) return null;
          return (await response.json()) as PackProfile;
        })
        .then((next) => {
          if (!next || cancel) return;
          cache = next;
          for (const [missionId, decision] of Object.entries(next.decisions)) {
            rememberDecision(missionId, decision);
          }
          setProfile(next);
        })
        .catch(() => undefined);
    };
    load();
    window.addEventListener(EVENT, load);
    return () => {
      cancel = true;
      window.removeEventListener(EVENT, load);
    };
  }, []);

  return profile;
}
