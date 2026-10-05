import { Link } from "react-router-dom";
import { usePackProfile } from "../api/profile";
import { useMissionList } from "../api/useMissionList";
import { Crest } from "../components/Crest";
import { IllustrativeChart } from "../components/IllustrativeChart";
import { Wordmark } from "../components/Wordmark";
import { QUIET_MARKET, TAGLINE } from "../copy";
import { rankProgress } from "../profile/ranks";

const TAPE = [
  { symbol: "BTC", price: "67,842.3", change: "+2.4%", points: "1,16 8,13 14,14 22,8 30,9 38,5 46,6" },
  { symbol: "ETH", price: "3,265.1", change: "+1.8%", points: "1,14 8,15 16,10 24,11 32,7 40,8 46,4" },
  { symbol: "SOL", price: "184.2", change: "+3.1%", points: "1,17 8,12 16,13 24,8 32,9 40,4 46,5" },
] as const;

export function HomeScreen() {
  const { missions, error } = useMissionList();
  const profile = usePackProfile();
  const open = missions?.filter((mission) => !mission.resolved) ?? [];
  const quiet = missions !== null && open.length === 0;
  const featured = open[0];
  const xp = profile ? profile.pack_xp : null;
  const progress = xp === null ? null : rankProgress(xp);

  return (
    <div className="home-faithful">
      <header className="home-hero">
        <img className="home-wolf" src="/brand/wolf-hero.webp" alt="" />
        <Wordmark size="xl" tone="paper" />
        <div className="home-hero-copy">
          <h1 className="home-hero-title">
            Markets move.
            <span>The Pack prepares.</span>
          </h1>
          <p className="home-standards">
            Higher standards
            <br />
            A brighter tomorrow
          </p>
          <p className="tagline home-tagline">{TAGLINE}</p>
        </div>
      </header>

      <section className="home-rank" aria-label="Wolf rank">
        <Crest size={46} />
        <div className="home-rank-id">
          <p className="kicker">Wolf rank</p>
          <p className="home-rank-name">{progress?.name ?? "N/A"}</p>
          <p className="fine">From the ledger</p>
        </div>
        <div className="home-xp">
          <div className="home-xp-row">
            <p className="kicker">Pack XP</p>
            <p className="home-xp-figure">
              {xp === null || !progress ? "N/A" : progress.nextAt !== null ? `${xp} / ${progress.nextAt}` : String(xp)}
            </p>
          </div>
          <div className="track" aria-hidden="true">
            <span style={{ width: `${progress ? Math.max(6, Math.round(progress.ratio * 100)) : 0}%` }} />
          </div>
          <p className="fine">
            {xp === null
              ? "Pack XP N/A until the ledger answers."
              : progress?.nextName && progress.nextAt !== null
                ? `${progress.nextAt - xp} XP to reach ${progress.nextName} · No cash value.`
                : "ELITE. Top of the climb. No cash value."}
          </p>
        </div>
      </section>

      <section className={quiet ? "home-tape quiet-hero" : "home-tape"}>
        <div className="home-tape-head">
          <p className="kicker">The quiet tape</p>
          <p className="fine">Den&apos;s read</p>
        </div>
        <p className="home-tape-copy">{QUIET_MARKET}</p>
        <ul className="home-markets">
          {TAPE.map((row) => (
            <li key={row.symbol}>
              <span className="home-ticker">{row.symbol}</span>
              <span className="home-price">{row.price}</span>
              <svg className="home-spark" viewBox="0 0 48 20" aria-hidden="true">
                <polyline points={row.points} />
              </svg>
              <span className="home-change">{row.change}</span>
            </li>
          ))}
        </ul>
        <div className="home-tape-foot">
          <p className="home-mock-note">MOCK · static design tape, not a live feed.</p>
          <div className="home-tape-links">
            <Link to="/replay">Run the tape</Link>
            {featured ? <Link to="/missions">Walk the board</Link> : null}
          </div>
        </div>
      </section>

      {error ? <p className="status-line">{error} Nothing was invented in its place.</p> : null}
      {!error && missions === null ? <p className="status-line">Reading fixtures…</p> : null}

      {featured ? (
        <section aria-label="Open missions">
          <article className="home-featured">
            <div className="home-featured-copy">
              <div className="home-featured-head">
                <p className="kicker">Featured mission</p>
                <span className="home-tf">
                  {featured.timeframe} {featured.direction}
                </span>
                <span className="home-pill">{featured.lifecycle_state}</span>
              </div>
              <h2 className="home-symbol">{featured.symbol}</h2>
              <p className="home-setup">{featured.title}</p>
              <p className="home-thesis">{featured.thesis_summary}</p>
            </div>
            <div className="home-featured-side">
              {featured.quality_tier === "HUNT" ? (
                <span className="home-pill is-fill">Hunt</span>
              ) : (
                <span className="home-pill">{featured.quality_tier}</span>
              )}
              <IllustrativeChart mission={featured} compact />
            </div>
            <div className="home-actions">
              <Link className="home-cta" to={`/missions/${encodeURIComponent(featured.cci_setup_id)}`}>
                <Crosshair />
                View mission
              </Link>
              <Link className="home-cta is-graphite" to={`/missions/${encodeURIComponent(featured.cci_setup_id)}#evidence`}>
                Market analysis
              </Link>
              <p className="home-motto">
                Plan
                <br />
                Execute
                <br />
                Journal
                <br />
                Improve
              </p>
            </div>
          </article>
          <p className="home-board">
            <span className="kicker">On the board</span>
            <span className="fine">The Pack has eyes on these.</span>
          </p>
        </section>
      ) : null}

      <section className="home-brand" aria-label="Quality over quantity">
        <Wordmark tone="paper" />
        <p className="home-quality">Quality &gt; quantity</p>
        <p className="fine home-infra">Information infrastructure for a higher standard</p>
      </section>
    </div>
  );
}

function Crosshair() {
  return (
    <svg className="btn-icon" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="6" />
      <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3" />
    </svg>
  );
}
