import { useEffect, useState } from "react";
import { fetchMissions } from "../api/missions";
import type { Mission } from "../api/types";
import { MissionCard } from "../components/MissionCard";
import { QUIET_MARKET } from "../copy";
import { useDecisions } from "../decisions/localDecisions";
import { telegramBridge } from "../telegram/TelegramBridge";

const FILTERS = ["OPEN", "RESOLVED", "MINE"] as const;
type MissionFilter = (typeof FILTERS)[number];

export function MissionsScreen() {
  const [filter, setFilter] = useState<MissionFilter>("OPEN");
  const [missions, setMissions] = useState<Mission[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const decisions = useDecisions();

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

  const visible = (missions ?? []).filter((mission) => {
    if (filter === "OPEN") return !mission.resolved;
    if (filter === "RESOLVED") return mission.resolved;
    return Boolean(decisions[mission.cci_setup_id]);
  });

  return (
    <div className="stack">
      <header>
        <p className="kicker">The board</p>
        <h1 className="display">Missions</h1>
        <p className="tagline">Open missions on the board. No invented urgency.</p>
      </header>
      <div className="filter-row" role="group" aria-label="Mission filters">
        {FILTERS.map((item) => (
          <button
            key={item}
            type="button"
            className="filter-chip"
            aria-pressed={filter === item}
            onClick={() => {
              setFilter(item);
              telegramBridge.selection();
            }}
          >
            {item}
          </button>
        ))}
      </div>
      {error ? <p className="status-line">{error} Nothing was invented in its place.</p> : null}
      {!error && missions === null ? <p className="status-line">Reading fixtures…</p> : null}
      {missions && visible.length === 0 ? (
        <section className="panel empty-panel">
          <p className="quiet-copy">
            {filter === "OPEN"
              ? QUIET_MARKET
              : filter === "MINE"
                ? "No calls sealed on this device yet."
                : "No closed fixtures in this preview."}
          </p>
        </section>
      ) : null}
      <div className="mission-list">
        {visible.map((mission) => (
          <MissionCard key={mission.cci_setup_id} mission={mission} />
        ))}
      </div>
    </div>
  );
}
