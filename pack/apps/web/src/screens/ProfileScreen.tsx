import { useMissionList } from "../api/useMissionList";
import { Crest } from "../components/Crest";
import { RankCard } from "../components/RankCard";
import { useDecisions } from "../decisions/localDecisions";
import { ACHIEVEMENTS, unlockedAchievementIds } from "../domain/achievements";
import { lockXp, replayPreview } from "../domain/xp";
import { WOLF_RANKS, rankProgress } from "../profile/ranks";
import { useJournals } from "../storage/journals";
import { useMarks } from "../storage/marks";
import { useReplays } from "../storage/replays";

const DEV_NAME = "Dev Operator";

export function ProfileScreen() {
  const decisions = useDecisions();
  const journals = useJournals();
  const replays = useReplays();
  const marks = useMarks();
  const { missions } = useMissionList();
  const locked = Object.entries(decisions);
  const noTrade = locked.filter(([, decision]) => decision === "NO_TRADE");
  const huntIds = new Set((missions ?? []).filter((mission) => mission.quality_tier === "HUNT").map((mission) => mission.cci_setup_id));
  const resolvedIds = new Set((missions ?? []).filter((mission) => mission.resolved).map((mission) => mission.cci_setup_id));
  const noTradeOnHunt = noTrade.filter(([missionId]) => huntIds.has(missionId)).length;
  const outcomeJournals = Object.keys(journals).filter((missionId) => resolvedIds.has(missionId)).length;
  const highScores = Object.values(replays).filter((attempt) => attempt.score >= 80).length;
  let xp = marks.evidenceRead.length * 5 + marks.reviews.length * 20;
  for (const [, decision] of locked) xp += lockXp(decision);
  for (const missionId of Object.keys(journals)) {
    if (resolvedIds.has(missionId)) xp += 30;
  }
  for (const attempt of Object.values(replays)) xp += replayPreview(attempt.score).total;
  const unlocked = unlockedAchievementIds({
    locks: locked.length,
    evidenceReads: marks.evidenceRead.length,
    journals: Object.keys(journals).length,
    noTrade: noTrade.length,
    noTradeOnHunt,
    reviews: marks.reviews.length,
    replays: Object.keys(replays).length,
    highScores,
    outcomeJournals,
  });
  const progress = rankProgress(xp);
  const ratio = locked.length === 0 ? "N/A" : `${noTrade.length}/${locked.length}`;

  return (
    <div className="stack">
      <header>
        <div className="rank-card-top">
          <Crest size={40} />
          <div>
            <p className="kicker">Your place in the Pack</p>
            <h1 className="display">Profile</h1>
          </div>
        </div>
        <p className="fine">{DEV_NAME} · dev preview name</p>
      </header>

      <RankCard xp={xp} />

      <section className="panel">
        <p className="kicker">Pack record</p>
        <ul className="rank-list">
          <li className="rank-item">
            <span>Calls sealed</span>
            <span>{locked.length} on this device</span>
          </li>
          <li className="rank-item">
            <span>NO TRADE ratio</span>
            <span>{ratio}</span>
          </li>
          <li className="rank-item">
            <span>Journals</span>
            <span>{Object.keys(journals).length}</span>
          </li>
          <li className="rank-item">
            <span>Tapes run</span>
            <span>{Object.keys(replays).length}</span>
          </li>
          <li className="rank-item">
            <span>Discipline streak</span>
            <span>N/A · placeholder</span>
          </li>
        </ul>
        <p className="fine">Discipline record only. No money on this shelf.</p>
      </section>

      <section className="panel" aria-label="Achievements">
        <div className="panel-head">
          <p className="kicker">Marks earned</p>
          <p className="fine">
            {unlocked.size}/{ACHIEVEMENTS.length}
          </p>
        </div>
        <ul className="achievement-grid">
          {ACHIEVEMENTS.map((card) => {
            const open = unlocked.has(card.id);
            return (
              <li key={card.id} className="achievement-card" data-unlocked={open ? "true" : "false"}>
                <p className="fine">{card.rarity}</p>
                <p className="section-title">{card.name}</p>
                <p className="fine">{open ? card.rule : "Locked"}</p>
              </li>
            );
          })}
        </ul>
      </section>

      <section className="panel">
        <p className="kicker">The climb</p>
        <ul className="rank-list">
          {WOLF_RANKS.map((rank) => (
            <li key={rank.name} className="rank-item" data-current={rank.name === progress.name ? "true" : "false"}>
              <span>{rank.name}</span>
              <span>{rank.xp} XP</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
