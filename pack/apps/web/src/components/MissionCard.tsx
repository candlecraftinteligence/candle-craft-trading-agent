import { Link } from "react-router-dom";
import type { Mission } from "../api/types";
import { toneForState } from "../presentation";

type MissionCardProps = {
  mission: Mission;
};

export function MissionCard({ mission }: MissionCardProps) {
  const tone = toneForState(mission.lifecycle_state);
  return (
    <Link className="mission-link" to={`/missions/${encodeURIComponent(mission.cci_setup_id)}`}>
      <article className="mission-card">
        <div className="mission-card-top">
          <span className="symbol">{mission.symbol}</span>
          {mission.quality_tier === "HUNT" ? (
            <span className="hunt-badge">Hunt</span>
          ) : (
            <span className="tier-standard">{mission.quality_tier}</span>
          )}
        </div>
        <div className="meta-row">
          <span>{mission.timeframe}</span>
          <span>{mission.direction}</span>
          <span className="state-chip" data-tone={tone}>
            {mission.lifecycle_state}
          </span>
        </div>
        <h2 className="card-title">{mission.title}</h2>
        <p className="card-thesis">{mission.thesis_summary}</p>
      </article>
    </Link>
  );
}
