import { useEffect, useState } from "react";
import { apiFetch } from "../api/server";
import type { Mission } from "../api/types";
import { ReplayDrill } from "../components/ReplayDrill";
import { REPLAY_DISCLAIMER } from "../copy";

type Tape = {
  cci_setup_id: string;
  timeframe: string;
  direction: string;
  masked_title: string;
  masked_thesis: string;
  evidence: Mission["evidence"];
  attempted: boolean;
  disclaimer: string;
};

export function ReplayScreen() {
  const { tapes, error } = useTapes();
  const [activeId, setActiveId] = useState<string | null>(null);
  const active = tapes?.find((tape) => tape.cci_setup_id === activeId) ?? null;

  if (active) {
    return <ReplayDrill mission={missionFromTape(active)} onExit={() => setActiveId(null)} />;
  }

  return (
    <div className="stack">
      <header className="page-hero">
        <img src="/brand/wolf-detail.webp" alt="" />
        <div>
          <p className="kicker">Training ground</p>
          <h1 className="display">Run the tape</h1>
          <p className="fine">{tapes ? `${tapes.length} closed tapes on the shelf` : "Reading fixtures…"}</p>
        </div>
      </header>

      <section className="panel">
        <p className="kicker">Last drill score</p>
        <p className="readout">—</p>
        <p className="fine">A drill score is pattern practice. It is not a profit record. The server scores the reveal.</p>
      </section>

      <p className="disclaimer" data-testid="replay-disclaimer">
        {REPLAY_DISCLAIMER}
      </p>

      {error ? <p className="status-line">{error} Nothing was invented in its place.</p> : null}

      <div className="mission-list">
        {(tapes ?? []).map((tape) => (
          <button
            key={tape.cci_setup_id}
            type="button"
            className="mission-card replay-card"
            onClick={() => setActiveId(tape.cci_setup_id)}
          >
            <div className="mission-card-top">
              <span className="symbol">Concealed perp</span>
              <span className="tier-standard">{tape.timeframe}</span>
            </div>
            <div className="meta-row">
              <span>{tape.direction}</span>
              <span>{tape.attempted ? "Drill logged" : "Sealed tape"}</span>
            </div>
            <h2 className="card-title">{tape.masked_title}</h2>
          </button>
        ))}
      </div>
    </div>
  );
}

function useTapes(): { tapes: Tape[] | null; error: string | null } {
  const [tapes, setTapes] = useState<Tape[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancel = false;
    apiFetch("/api/replay")
      .then(async (response) => {
        if (!response.ok) throw new Error("Replay is unavailable.");
        return (await response.json()) as { tapes?: Tape[] };
      })
      .then((body) => {
        if (!cancel) setTapes(body.tapes ?? []);
      })
      .catch((reason: unknown) => {
        if (!cancel) setError(reason instanceof Error ? reason.message : "Replay is unavailable.");
      });
    return () => {
      cancel = true;
    };
  }, []);

  return { tapes, error };
}

function missionFromTape(tape: Tape): Mission {
  return {
    cci_setup_id: tape.cci_setup_id,
    symbol: "Concealed perp",
    timeframe: tape.timeframe,
    direction: tape.direction,
    quality_tier: "STANDARD",
    title: tape.masked_title,
    thesis_summary: tape.masked_thesis,
    evidence: tape.evidence,
    lifecycle: [],
    lifecycle_state: "CLOSED",
    outcome_code: null,
    opened_at: "",
    resolved_at: null,
    resolved: true,
    synthetic: true,
    disclaimer: tape.disclaimer,
    replay: {
      masked_title: tape.masked_title,
      masked_thesis: tape.masked_thesis,
      teaching_note: "",
      preferred_decision: "TRACK",
      evidence: tape.evidence,
    },
  };
}
