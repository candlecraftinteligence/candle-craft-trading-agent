import { useState } from "react";
import { useMissionList } from "../api/useMissionList";
import { ReplayDrill } from "../components/ReplayDrill";
import { REPLAY_DISCLAIMER } from "../copy";
import { useReplays } from "../storage/replays";

export function ReplayScreen() {
  const { missions, error } = useMissionList();
  const replays = useReplays();
  const [activeId, setActiveId] = useState<string | null>(null);
  const closed = (missions ?? []).filter((mission) => mission.resolved && mission.replay);
  const active = closed.find((mission) => mission.cci_setup_id === activeId) ?? null;
  const scores = Object.values(replays).map((attempt) => attempt.score);
  const lastScore = scores.length > 0 ? scores[scores.length - 1] : null;

  if (active) {
    return <ReplayDrill mission={active} onExit={() => setActiveId(null)} />;
  }

  return (
    <div className="stack">
      <header>
        <p className="kicker">Training ground</p>
        <h1 className="display">Run the tape</h1>
        <p className="fine">{missions ? `${closed.length} closed tapes on the shelf` : "Reading fixtures…"}</p>
      </header>

      <section className="panel">
        <p className="kicker">Last drill score</p>
        <p className="readout">{lastScore === null ? "—" : lastScore}</p>
        <p className="fine">A drill score is pattern practice. It is not a profit record.</p>
      </section>

      <p className="disclaimer">{REPLAY_DISCLAIMER}</p>

      {error ? <p className="status-line">{error} Nothing was invented in its place.</p> : null}

      <div className="mission-list">
        {closed.map((mission) => (
          <button
            key={mission.cci_setup_id}
            type="button"
            className="mission-card replay-card"
            onClick={() => setActiveId(mission.cci_setup_id)}
          >
            <div className="mission-card-top">
              <span className="symbol">Concealed perp</span>
              <span className="tier-standard">{mission.timeframe}</span>
            </div>
            <div className="meta-row">
              <span>{mission.direction}</span>
              <span>{replays[mission.cci_setup_id] ? "Drill logged" : "Sealed tape"}</span>
            </div>
            <h2 className="card-title">{mission.replay?.masked_title}</h2>
          </button>
        ))}
      </div>
    </div>
  );
}
