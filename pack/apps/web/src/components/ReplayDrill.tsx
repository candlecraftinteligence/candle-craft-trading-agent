import { motion, useReducedMotion } from "framer-motion";
import { useState } from "react";
import type { Mission } from "../api/types";
import { REPLAY_DISCLAIMER } from "../copy";
import { replayPreview } from "../domain/xp";
import {
  TRAINING_DECISIONS,
  scoreReplay,
  type QualityChoice,
  type TrainingDecision,
} from "../domain/replayScore";
import { saveReplay, useReplays } from "../storage/replays";
import { LifecycleTimeline } from "./LifecycleTimeline";
import { XpPreview } from "./XpPreview";

type ReplayDrillProps = {
  mission: Mission;
  onExit: () => void;
};

export function ReplayDrill({ mission, onExit }: ReplayDrillProps) {
  const brief = mission.replay;
  const stored = useReplays()[mission.cci_setup_id];
  const reduced = useReducedMotion();
  const [tier, setTier] = useState<QualityChoice | null>(stored?.chosenTier ?? null);
  const [decision, setDecision] = useState<TrainingDecision | null>(stored?.chosenDecision ?? null);
  const [reviewed, setReviewed] = useState(Boolean(stored));
  const [revealed, setRevealed] = useState(Boolean(stored));

  if (!brief) {
    return <p className="status-line">This fixture has no replay brief.</p>;
  }

  const preferred = brief.preferred_decision;
  const ready = Boolean(tier && decision && reviewed);
  const score = revealed && tier && decision
    ? scoreReplay({
        actualTier: mission.quality_tier,
        chosenTier: tier,
        preferred,
        chosen: decision,
        evidenceReviewed: true,
      })
    : null;
  const xp = score ? replayPreview(score.total) : null;

  function reveal() {
    if (!tier || !decision || !reviewed || !isTrainingDecision(preferred)) return;
    const next = scoreReplay({
      actualTier: mission.quality_tier,
      chosenTier: tier,
      preferred,
      chosen: decision,
      evidenceReviewed: reviewed,
    });
    saveReplay({
      missionId: mission.cci_setup_id,
      chosenTier: tier,
      chosenDecision: decision,
      score: next.total,
    });
    setRevealed(true);
  }

  return (
    <div className="stack">
      <button type="button" className="back-link" onClick={onExit}>
        Back to the tapes
      </button>
      <header>
        <p className="kicker">Training ground · not a live hunt</p>
        <h1 className="display">{revealed ? mission.symbol : "Concealed perp"}</h1>
        <div className="meta-row">
          <span>{mission.timeframe}</span>
          <span>{mission.direction}</span>
          {revealed ? <span className="tier-standard">{mission.quality_tier}</span> : <span>Quality hidden</span>}
        </div>
      </header>

      <p className="disclaimer" data-testid="replay-disclaimer">
        {REPLAY_DISCLAIMER}
      </p>

      {!revealed ? (
        <>
          <section className="panel">
            <p className="kicker">The tape, masked</p>
            <h2 className="section-title">{brief.masked_title}</h2>
            <p className="body-copy">{brief.masked_thesis}</p>
          </section>
          <section className="panel">
            <p className="kicker">Study the evidence</p>
            <ul className="evidence-list">
              {brief.evidence.map((block) => (
                <li key={`${block.type}-${block.label}`} className="evidence-item">
                  <div className="evidence-type">{block.type}</div>
                  <p className="section-title">{block.label}</p>
                  {block.detail ? <p className="body-copy">{block.detail}</p> : null}
                </li>
              ))}
            </ul>
            <label className="check-row">
              <input
                type="checkbox"
                checked={reviewed}
                onChange={(event) => setReviewed(event.target.checked)}
              />
              Evidence reviewed
            </label>
          </section>
          <section className="panel">
            <p className="kicker">Name the tier</p>
            <div className="decision-grid">
              {(["HUNT", "STANDARD"] as const).map((choice) => (
                <button
                  key={choice}
                  type="button"
                  className="decision-btn"
                  aria-pressed={tier === choice}
                  onClick={() => setTier(choice)}
                >
                  {choice}
                </button>
              ))}
            </div>
          </section>
          <section className="panel">
            <p className="kicker">Your drill call</p>
            <p className="fine">A drill call. It does not seal a live mission.</p>
            <div className="decision-grid">
              {TRAINING_DECISIONS.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="decision-btn"
                  aria-pressed={decision === item.id}
                  onClick={() => setDecision(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <button type="button" className="btn primary reveal-btn" disabled={!ready} onClick={reveal}>
              Reveal
            </button>
          </section>
        </>
      ) : (
        <motion.div
          className="stack"
          data-testid="replay-reveal"
          initial={reduced ? false : { opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: reduced ? 0 : 0.24 }}
        >
          <section className="panel">
            <p className="kicker">The tape, after</p>
            <LifecycleTimeline events={mission.lifecycle} />
          </section>
          <section className="panel outcome-block" data-testid="replay-outcome">
            <p className="kicker">CCI Outcome</p>
            <p className="outcome-code">{mission.outcome_code ?? "No outcome code in this fixture."}</p>
          </section>
          <section className="panel">
            <p className="kicker">What the tape taught</p>
            <p className="body-copy">{brief.teaching_note}</p>
          </section>
          {score ? (
            <section className="panel" aria-label="Training score">
              <p className="kicker">Drill score</p>
              <p className="readout">{score.total}</p>
              <ul className="rank-list">
                <li className="rank-item">
                  <span>Quality recognition</span>
                  <span>{score.quality}/40</span>
                </li>
                <li className="rank-item">
                  <span>Decision alignment</span>
                  <span>{score.decision}/40</span>
                </li>
                <li className="rank-item">
                  <span>Evidence attention</span>
                  <span>{score.attention}/20</span>
                </li>
              </ul>
              <p className="fine">A drill score is not evidence of profitability.</p>
            </section>
          ) : null}
          {xp ? <XpPreview lines={xp.lines} total={xp.total} /> : null}
        </motion.div>
      )}

    </div>
  );
}

function isTrainingDecision(value: string): value is TrainingDecision {
  return TRAINING_DECISIONS.some((item) => item.id === value);
}
