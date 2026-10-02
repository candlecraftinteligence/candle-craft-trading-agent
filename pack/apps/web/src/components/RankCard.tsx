import { Crest } from "./Crest";
import { WOLF_RANKS, rankProgress } from "../profile/ranks";

type RankCardProps = {
  xp: number | null;
  ladder?: boolean;
};

export function RankCard({ xp, ladder = false }: RankCardProps) {
  const progress = xp === null ? null : rankProgress(xp);
  const currentIndex = progress ? WOLF_RANKS.findIndex((rank) => rank.name === progress.name) : -1;
  return (
    <section className={ladder ? "panel rank-card with-ladder" : "panel rank-card rank-compact"} aria-label="Wolf rank">
      <div className="rank-card-main">
        <div className="rank-card-top">
          <Crest size={40} />
          <div>
            <p className="kicker">Wolf Rank</p>
            <p className="readout">{progress?.name ?? "N/A"}</p>
            <p className="fine">From the ledger</p>
          </div>
        </div>
        <div className="xp-block">
          <div className="panel-head">
            <p className="kicker">Pack XP</p>
            <p className="fine">
              {xp === null ? "N/A" : progress?.nextAt !== null ? `${xp} / ${progress?.nextAt}` : String(xp)}
            </p>
          </div>
          <div className="track" aria-hidden="true">
            <span style={{ width: `${progress ? Math.round(progress.ratio * 100) : 0}%` }} />
          </div>
          <p className="fine">
            {xp === null
              ? "Pack XP N/A until the ledger answers."
              : progress?.nextName && progress.nextAt !== null
                ? `${progress.nextAt - xp} XP to reach ${progress.nextName}`
                : "ELITE. Top of the climb."}
          </p>
          <p className="fine">No cash value.</p>
        </div>
      </div>
      {ladder ? (
        <ol className="rank-ladder">
          {WOLF_RANKS.map((rank, index) => (
            <li
              key={rank.name}
              data-state={index === currentIndex ? "current" : index < currentIndex ? "passed" : "ahead"}
            >
              {rank.name}
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}
