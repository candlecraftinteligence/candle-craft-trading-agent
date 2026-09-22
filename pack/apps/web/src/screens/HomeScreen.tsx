import { Link } from "react-router-dom";
import { usePackProfile } from "../api/profile";
import { useMissionList } from "../api/useMissionList";
import { IllustrativeChart } from "../components/IllustrativeChart";
import { RankCard } from "../components/RankCard";
import { TodaysQuests } from "../components/TodaysQuests";
import { Wordmark } from "../components/Wordmark";
import { QUIET_MARKET, TAGLINE } from "../copy";

export function HomeScreen() {
  const { missions, error } = useMissionList();
  const profile = usePackProfile();
  const open = missions?.filter((mission) => !mission.resolved) ?? [];
  const quiet = missions !== null && open.length === 0;
  const featured = open[0];

  return (
    <div className="stack">
      <header className="hero-band">
        <img className="hero-wolf" src="/brand/wolf-home.webp" alt="" />
        <div className="hero-copy">
          <Wordmark />
          <p className="hero-kicker">Discipline creates clarity</p>
          <h1 className="hero-title">
            Markets move.
            <span>The Pack prepares.</span>
          </h1>
          <p className="hero-sub">Higher standards. A brighter tomorrow.</p>
          <p className="tagline">{TAGLINE}</p>
        </div>
      </header>

      <RankCard xp={profile ? profile.pack_xp : null} />

      <section className={quiet ? "panel quiet-hero mountain-card" : "panel mountain-card"}>
        <div className="panel-head">
          <p className="kicker">The quiet tape</p>
          <p className="fine">Den&apos;s read</p>
        </div>
        <p className="fine">Den&apos;s quiet. Ears stay up.</p>
        <p className="quiet-copy">{QUIET_MARKET}</p>
        <div className="actions">
          <Link className="btn primary" to="/replay">
            Run the tape
          </Link>
          {quiet ? null : (
            <Link className="btn" to="/missions">
              Walk the board
            </Link>
          )}
        </div>
      </section>

      {error ? <p className="status-line">{error} Nothing was invented in its place.</p> : null}
      {!error && missions === null ? <p className="status-line">Reading fixtures…</p> : null}

      {featured ? (
        <section className="stack" aria-label="Open missions">
          <article className="featured-mission">
            <div className="featured-copy">
              <div className="panel-head">
                <p className="kicker">On the board</p>
                <span className="state-chip" data-tone="active">
                  {featured.lifecycle_state}
                </span>
              </div>
              <p className="fine">The Pack has eyes on these.</p>
              <div className="meta-row">
                <span>
                  {featured.timeframe} {featured.direction}
                </span>
                {featured.quality_tier === "HUNT" ? <span className="hunt-badge">Hunt</span> : <span className="tier-standard">{featured.quality_tier}</span>}
              </div>
              <h2 className="featured-symbol">{featured.symbol}</h2>
              <p className="card-title">{featured.title}</p>
              <Link className="btn primary" to={`/missions/${encodeURIComponent(featured.cci_setup_id)}`}>
                View mission
              </Link>
              <p className="card-thesis">{featured.thesis_summary}</p>
            </div>
            <IllustrativeChart mission={featured} compact />
          </article>
        </section>
      ) : null}

      <TodaysQuests />

      <section className="quality-strip" aria-label="Quality over quantity">
        <Wordmark />
        <p>Quality &gt; quantity</p>
        <p className="fine">Information infrastructure for a higher standard</p>
      </section>
    </div>
  );
}
