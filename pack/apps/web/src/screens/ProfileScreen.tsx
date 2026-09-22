import { invalidatePackProfile, usePackProfile, type NotificationPrefs } from "../api/profile";
import { invalidateQuests, useUnlockedAchievements } from "../api/quests";
import { apiFetch } from "../api/server";
import { RankCard } from "../components/RankCard";
import { QUIET_MARKET, TAGLINE } from "../copy";
import { ACHIEVEMENTS } from "../domain/achievements";

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
  const ratio = !profile || locked.length === 0 ? "N/A" : `${profile.no_trade_count}/${locked.length}`;
  const prefs = profile?.notification_prefs ?? null;

  return (
    <div className="stack">
      <header className="hero-band profile-hero">
        <img className="hero-wolf" src="/brand/wolf-profile.webp" alt="" />
        <div className="hero-copy">
          <p className="kicker">Your place in the Pack</p>
          <h1 className="display">{profile?.display_name ?? "N/A"}</h1>
          <p className="fine">Trader / builder / Pack member</p>
          <p className="tagline">{TAGLINE}</p>
        </div>
      </header>

      <RankCard xp={xp} ladder />

      <section className="panel">
        <div className="panel-head">
          <p className="kicker">Pack record</p>
          <p className="fine">Discipline wins</p>
        </div>
        <ul className="record-row">
          <li>
            <RecordIcon kind="calls" />
            <strong>{profile ? locked.length : "N/A"}</strong>
            <span>Calls sealed</span>
          </li>
          <li>
            <RecordIcon kind="pass" />
            <strong>{ratio}</strong>
            <span>NO TRADE ratio</span>
          </li>
          <li>
            <RecordIcon kind="journal" />
            <strong>{profile ? journalIds.length : "N/A"}</strong>
            <span>Journals</span>
          </li>
          <li>
            <RecordIcon kind="tape" />
            <strong>{profile ? profile.replay_count : "N/A"}</strong>
            <span>Tapes run</span>
          </li>
          <li>
            <RecordIcon kind="streak" />
            <strong>{profile ? profile.discipline_streak : "N/A"}</strong>
            <span>Discipline streak</span>
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
        <ul className="hex-grid">
          {ACHIEVEMENTS.map((card) => {
            const open = unlocked.has(card.id);
            return (
              <li key={card.id} className="hex-tile" data-unlocked={open ? "true" : "false"} title={open ? card.rule : "Locked"}>
                <span className="hex-name">{card.name}</span>
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

      <section className="mountain-footer">
        <p className="kicker">The Pack waits</p>
        <p className="quiet-copy">{QUIET_MARKET}</p>
      </section>
    </div>
  );
}

function RecordIcon({ kind }: { kind: "calls" | "pass" | "journal" | "tape" | "streak" }) {
  return (
    <svg className="record-icon" viewBox="0 0 24 24" aria-hidden="true">
      {kind === "calls" ? <circle cx="12" cy="12" r="7" /> : null}
      {kind === "pass" ? <path d="M6 12h12M12 6v12" /> : null}
      {kind === "journal" ? <path d="M7 4h8l3 3v13H7zM15 4v4h4" /> : null}
      {kind === "tape" ? <path d="M8 7v10l9-5-9-5Z" /> : null}
      {kind === "streak" ? <path d="M12 4c2 4 4 5 4 8a4 4 0 1 1-8 0c0-3 2-4 4-8Z" /> : null}
    </svg>
  );
}
