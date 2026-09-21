import { motion, useReducedMotion } from "framer-motion";
import { Link } from "react-router-dom";
import { useEffect, useState } from "react";
import { fetchMissions } from "../api/missions";
import type { Mission } from "../api/types";
import { MissionCard } from "../components/MissionCard";
import { PRODUCT_NAME, QUIET_MARKET, TAGLINE } from "../copy";
import { NEXT_RANK, PLACEHOLDER_RANK, PLACEHOLDER_XP } from "../profile/ranks";

export function HomeScreen() {
  const reduced = useReducedMotion();
  const { missions, error } = useMissionIndex();
  const open = missions?.filter((mission) => !mission.resolved) ?? [];

  return (
    <div className="stack">
      <header>
        <p className="kicker">Command</p>
        <h1 className="display">{PRODUCT_NAME}</h1>
        <p className="tagline">{TAGLINE}</p>
      </header>

      <section className="panel" aria-label="Wolf rank placeholder">
        <div className="panel-head">
          <p className="kicker">Wolf Rank</p>
          <p className="fine">Placeholder</p>
        </div>
        <p className="readout">{PLACEHOLDER_RANK}</p>
        <p className="fine">Pack XP {PLACEHOLDER_XP} · no cash value</p>
        <div className="track" aria-hidden="true">
          <span />
        </div>
        <p className="fine">
          Next {NEXT_RANK.name} at {NEXT_RANK.xp} Pack XP. Rank is cosmetic.
        </p>
      </section>

      <section className="panel">
        <p className="kicker">Quiet tape</p>
        <p className="quiet-copy">{QUIET_MARKET}</p>
        <div className="actions">
          <Link className="btn primary" to="/replay">
            Train in Replay
          </Link>
          <Link className="btn" to="/missions">
            Review Missions
          </Link>
        </div>
      </section>

      <section className="stack" aria-label="Open missions">
        <div className="panel-head">
          <p className="kicker">Open missions</p>
          <p className="fine">{missions ? String(open.length) : "—"}</p>
        </div>
        {error ? <p className="status-line">{error} Nothing was invented in its place.</p> : null}
        {!error && missions === null ? <p className="status-line">Reading fixtures…</p> : null}
        {missions && open.length === 0 ? (
          <p className="status-line">No open missions in the fixture set.</p>
        ) : null}
        {open.slice(0, 2).map((mission, index) => (
          <motion.div
            key={mission.cci_setup_id}
            initial={reduced ? false : { opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: reduced ? 0 : 0.22, delay: reduced ? 0 : index * 0.04 }}
          >
            <MissionCard mission={mission} />
          </motion.div>
        ))}
      </section>
    </div>
  );
}

function useMissionIndex(): { missions: Mission[] | null; error: string | null } {
  const [missions, setMissions] = useState<Mission[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchMissions()
      .then((rows) => {
        if (!cancelled) setMissions(rows);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "Missions are unavailable.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { missions, error };
}
