import { Crest } from "./Crest";
import { rankProgress } from "../profile/ranks";

type RankCardProps = {
  xp: number | null;
};

export function RankCard({ xp }: RankCardProps) {
  const progress = xp === null ? null : rankProgress(xp);
  return (
    <section className="panel rank-card" aria-label="Wolf rank">
      <div className="rank-card-top">
        <Crest size={44} />
        <div>
          <div className="panel-head">
            <p className="kicker">Wolf Rank</p>
            <p className="fine">From the ledger</p>
          </div>
          <p className="readout">{progress?.name ?? "N/A"}</p>
        </div>
      </div>
      <p className="fine">Pack XP {xp === null ? "N/A" : xp} · no cash value</p>
      <div className="track" aria-hidden="true">
        <span style={{ width: `${progress ? Math.round(progress.ratio * 100) : 0}%` }} />
      </div>
      <p className="fine">
        {xp === null
          ? "Pack XP N/A until the ledger answers."
          : progress?.nextName && progress.nextAt !== null
            ? `${xp} / ${progress.nextAt} to reach ${progress.nextName}`
            : "ELITE. Top of the climb."}
      </p>
    </section>
  );
}
