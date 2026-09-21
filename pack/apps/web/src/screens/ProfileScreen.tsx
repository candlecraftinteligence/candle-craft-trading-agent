import { usePackProfile } from "../api/profile";
import { useMissionList } from "../api/useMissionList";
import { Crest } from "../components/Crest";
import { RankCard } from "../components/RankCard";
import { ACHIEVEMENTS, unlockedAchievementIds } from "../domain/achievements";
import { WOLF_RANKS, rankProgress } from "../profile/ranks";

export function ProfileScreen() {
  const profile = usePackProfile();
  const { missions } = useMissionList();
  const decisions = profile?.decisions ?? {};
  const locked = Object.entries(decisions);
  const huntIds = new Set((missions ?? []).filter((mission) => mission.quality_tier === "HUNT").map((mission) => mission.cci_setup_id));
  const resolvedIds = new Set((missions ?? []).filter((mission) => mission.resolved).map((mission) => mission.cci_setup_id));
  const noTradeOnHunt = locked.filter(([missionId, decision]) => decision === "NO_TRADE" && huntIds.has(missionId)).length;
  const journalIds = profile?.journal_ids ?? [];
  const outcomeJournals = journalIds.filter((missionId) => resolvedIds.has(missionId)).length;
  const xp = profile ? profile.pack_xp : null;
  const unlocked = profile
    ? unlockedAchievementIds({
        locks: locked.length,
        evidenceReads: 0,
        journals: journalIds.length,
        noTrade: profile.no_trade_count,
        noTradeOnHunt,
        reviews: 0,
        replays: profile.replay_count,
        highScores: 0,
        outcomeJournals,
      })
    : new Set<string>();
  const progress = xp === null ? null : rankProgress(xp);
  const ratio = !profile || locked.length === 0 ? "N/A" : `${profile.no_trade_count}/${locked.length}`;

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
        <p className="fine">{profile?.display_name ?? "N/A"}</p>
      </header>

      <RankCard xp={xp} />

      <section className="panel">
        <p className="kicker">Pack record</p>
        <ul className="rank-list">
          <li className="rank-item">
            <span>Calls sealed</span>
            <span>{profile ? locked.length : "N/A"}</span>
          </li>
          <li className="rank-item">
            <span>NO TRADE ratio</span>
            <span>{ratio}</span>
          </li>
          <li className="rank-item">
            <span>Journals</span>
            <span>{profile ? journalIds.length : "N/A"}</span>
          </li>
          <li className="rank-item">
            <span>Tapes run</span>
            <span>{profile ? profile.replay_count : "N/A"}</span>
          </li>
          <li className="rank-item">
            <span>Discipline streak</span>
            <span>{profile ? profile.discipline_streak : "N/A"}</span>
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
            <li key={rank.name} className="rank-item" data-current={progress && rank.name === progress.name ? "true" : "false"}>
              <span>{rank.name}</span>
              <span>{rank.xp} XP</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
