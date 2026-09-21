import { invalidatePackProfile, usePackProfile, type NotificationPrefs } from "../api/profile";
import { invalidateQuests, useUnlockedAchievements } from "../api/quests";
import { apiFetch } from "../api/server";
import { Crest } from "../components/Crest";
import { RankCard } from "../components/RankCard";
import { ACHIEVEMENTS } from "../domain/achievements";
import { WOLF_RANKS, rankProgress } from "../profile/ranks";

const PREF_ROWS: { key: keyof NotificationPrefs; label: string; hint: string }[] = [
  { key: "new_mission", label: "New mission", hint: "On unless you turn it off" },
  { key: "lifecycle_resolution", label: "Resolution", hint: "On unless you turn it off" },
  { key: "quest_complete", label: "Drill logged", hint: "Quiet until you ask" },
  { key: "streak", label: "Discipline streak", hint: "Quiet until you ask" },
  { key: "replay_nudge", label: "Replay nudge", hint: "Quiet until you ask" },
];

export function ProfileScreen() {
  const profile = usePackProfile();
  const unlocked = new Set(useUnlockedAchievements() ?? []);
  const decisions = profile?.decisions ?? {};
  const locked = Object.entries(decisions);
  const journalIds = profile?.journal_ids ?? [];
  const xp = profile ? profile.pack_xp : null;
  const progress = xp === null ? null : rankProgress(xp);
  const ratio = !profile || locked.length === 0 ? "N/A" : `${profile.no_trade_count}/${locked.length}`;
  const prefs = profile?.notification_prefs ?? null;

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
        {profile?.oath_accepted ? null : (
          <button
            type="button"
            className="btn"
            onClick={() => {
              void apiFetch("/api/me/oath", { method: "POST", body: "{}" }).then((response) => {
                if (!response.ok) return;
                invalidateQuests();
                invalidatePackProfile();
              });
            }}
          >
            Take the Pack oath
          </button>
        )}
      </section>

      <section className="panel" aria-label="Notification preferences">
        <p className="kicker">Den signals</p>
        <p className="fine">Mission alerts start on. Drill, streak, and Replay nudges start off.</p>
        {prefs ? (
          <ul className="rank-list">
            {PREF_ROWS.map((row) => (
              <li key={row.key} className="rank-item">
                <span>
                  {row.label}
                  <span className="fine"> {row.hint}</span>
                </span>
                <button
                  type="button"
                  className="btn"
                  aria-pressed={prefs[row.key]}
                  onClick={() => {
                    const next = { ...prefs, [row.key]: !prefs[row.key] };
                    void apiFetch("/api/me/notification-prefs", {
                      method: "POST",
                      body: JSON.stringify(next),
                    }).then((response) => {
                      if (response.ok) invalidatePackProfile();
                    });
                  }}
                >
                  {prefs[row.key] ? "On" : "Off"}
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="fine">N/A</p>
        )}
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
