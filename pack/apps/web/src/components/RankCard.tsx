import { Crest } from "./Crest";
import { rankProgress } from "../profile/ranks";

type RankCardProps = {
  xp: number;
};

export function RankCard({ xp }: RankCardProps) {
  const progress = rankProgress(xp);
  return (
    <section className="panel rank-card" aria-label="Wolf rank">
      <div className="rank-card-top">
        <Crest size={44} />
        <div>
          <div className="panel-head">
            <p className="kicker">Wolf Rank</p>
            <p className="fine">Cosmetic climb</p>
          </div>
          <p className="readout">{progress.name}</p>
        </div>
      </div>
      <p className="fine">Pack XP {xp} · a cosmetic climb · no cash value</p>
      <div className="track" aria-hidden="true">
        <span style={{ width: `${Math.round(progress.ratio * 100)}%` }} />
      </div>
      <p className="fine">
        {progress.nextName && progress.nextAt !== null
          ? `${xp} / ${progress.nextAt} to reach ${progress.nextName}`
          : "ELITE. Top of the cosmetic climb."}
      </p>
    </section>
  );
}
