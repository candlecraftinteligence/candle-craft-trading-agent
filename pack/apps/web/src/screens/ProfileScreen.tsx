import { useDecisions } from "../decisions/localDecisions";
import { NEXT_RANK, PLACEHOLDER_RANK, PLACEHOLDER_XP, WOLF_RANKS } from "../profile/ranks";

export function ProfileScreen() {
  const decisions = useDecisions();
  const locked = Object.values(decisions);
  const noTrade = locked.filter((decision) => decision === "NO_TRADE").length;
  const ratio = locked.length === 0 ? "N/A" : `${noTrade}/${locked.length}`;

  return (
    <div className="stack">
      <header>
        <p className="kicker">Identity</p>
        <h1 className="display">Profile</h1>
      </header>

      <section className="panel" aria-label="Wolf rank placeholder">
        <div className="panel-head">
          <p className="kicker">Wolf Rank</p>
          <p className="fine">Placeholder</p>
        </div>
        <p className="readout">{PLACEHOLDER_RANK}</p>
        <p className="fine">
          Pack XP {PLACEHOLDER_XP} · cosmetic only · no cash value
        </p>
        <div className="track" aria-hidden="true">
          <span />
        </div>
        <p className="fine">
          {PLACEHOLDER_XP} / {NEXT_RANK.xp} toward {NEXT_RANK.name}
        </p>
      </section>

      <section className="panel">
        <p className="kicker">Process</p>
        <ul className="rank-list">
          <li className="rank-item">
            <span>Missions locked</span>
            <span>{locked.length} on this device</span>
          </li>
          <li className="rank-item">
            <span>NO TRADE ratio</span>
            <span>{ratio}</span>
          </li>
          <li className="rank-item">
            <span>Journals</span>
            <span>N/A</span>
          </li>
          <li className="rank-item">
            <span>Replays</span>
            <span>N/A</span>
          </li>
          <li className="rank-item">
            <span>Discipline streak</span>
            <span>N/A</span>
          </li>
        </ul>
      </section>

      <section className="panel">
        <p className="kicker">Rank path</p>
        <ul className="rank-list">
          {WOLF_RANKS.map((rank) => (
            <li
              key={rank.name}
              className="rank-item"
              data-current={rank.name === PLACEHOLDER_RANK ? "true" : "false"}
            >
              <span>{rank.name}</span>
              <span>{rank.xp} XP</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
