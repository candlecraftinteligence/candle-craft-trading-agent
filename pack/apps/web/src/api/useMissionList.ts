import { useEffect, useState } from "react";
import { fetchMissions } from "./missions";
import type { Mission } from "./types";

export function useMissionList(): { missions: Mission[] | null; error: string | null } {
  const [missions, setMissions] = useState<Mission[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchMissions()
      .then((rows) => {
        if (!cancelled) setMissions(rows);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "Missions are unavailable.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { missions, error };
}
