import { motion, useReducedMotion } from "framer-motion";
import { Link } from "react-router-dom";
import { usePackProfile } from "../api/profile";
import { useMissionList } from "../api/useMissionList";
import { MissionCard } from "../components/MissionCard";
import { RankCard } from "../components/RankCard";
import { TodaysQuests } from "../components/TodaysQuests";
import { PRODUCT_NAME, QUIET_MARKET, TAGLINE } from "../copy";

export function HomeScreen() {
  const reduced = useReducedMotion();
  const { missions, error } = useMissionList();
  const profile = usePackProfile();
  const open = missions?.filter((mission) => !mission.resolved) ?? [];
  const quiet = missions !== null && open.length === 0;

  return (
    <div className="stack">
      <header>
        <p className="kicker">Pack den</p>
        <h1 className="display">{PRODUCT_NAME}</h1>
        <p className="tagline">{TAGLINE}</p>
      </header>

      <RankCard xp={profile ? profile.pack_xp : null} />

      {quiet ? (
        <section className="panel quiet-hero">
          <p className="kicker">Quiet tape</p>
          <p className="fine">Den's quiet. Ears stay up.</p>
          <p className="quiet-copy">{QUIET_MARKET}</p>
          <div className="actions">
            <Link className="btn primary" to="/replay">
              Run the tape
            </Link>
          </div>
        </section>
      ) : (
        <section className="panel">
          <p className="kicker">Quiet tape</p>
          <p className="fine">Den's quiet. Ears stay up.</p>
          <p className="quiet-copy">{QUIET_MARKET}</p>
          <div className="actions">
            <Link className="btn primary" to="/replay">
              Run the tape
            </Link>
            <Link className="btn" to="/missions">
              Walk the board
            </Link>
          </div>
        </section>
      )}

      {error ? <p className="status-line">{error} Nothing was invented in its place.</p> : null}
      {!error && missions === null ? <p className="status-line">Reading fixtures…</p> : null}

      {!quiet && missions ? (
        <section className="stack" aria-label="Open missions">
          <div className="panel-head">
            <p className="kicker">On the board</p>
            <p className="fine">{String(open.length)}</p>
          </div>
          <p className="fine">The Pack has eyes on these.</p>
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
      ) : null}

      <TodaysQuests />
    </div>
  );
}
