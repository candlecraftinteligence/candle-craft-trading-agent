import { useState } from "react";
import {
  DECISIONS,
  decisionLabel,
  lockDecision,
  useDecisions,
  type DecisionId,
} from "../decisions/localDecisions";
import { telegramBridge } from "../telegram/TelegramBridge";

const SEAL_LINE: Record<DecisionId, string> = {
  TRACK: "Marked. Stay sharp.",
  I_TOOK_THIS: "Logged. Journal the risk.",
  WATCH_ONLY: "Eyes on. No fill claimed.",
  NO_TRADE: "Passing is Pack strength.",
};

type DecisionPanelProps = {
  missionId: string;
};

export function DecisionPanel({ missionId }: DecisionPanelProps) {
  const decisions = useDecisions();
  const locked = decisions[missionId] ?? null;
  const [note, setNote] = useState<string | null>(null);

  function choose(decision: DecisionId) {
    if (locked) return;
    const stored = lockDecision(missionId, decision);
    telegramBridge.impact("medium");
    setNote(
      stored === decision
        ? `${SEAL_LINE[stored]} Sealed on this device.`
        : `Already sealed · ${decisionLabel(stored)}`,
    );
  }

  return (
    <section className="decision-panel" aria-label="Decision lock">
      <div className="panel-head">
        <p className="kicker">Your call</p>
        <p className="fine">{locked ? "Sealed" : "Once"}</p>
      </div>
      {locked ? (
        <div className="lock-seal" data-testid="decision-lock">
          <p className="kicker">Sealed on this device</p>
          <p className="lock-choice">{decisionLabel(locked)}</p>
        </div>
      ) : null}
      <div className="decision-grid">
        {DECISIONS.map((decision) => (
          <button
            key={decision.id}
            type="button"
            className="decision-btn"
            aria-pressed={locked === decision.id}
            disabled={locked !== null}
            onClick={() => choose(decision.id)}
          >
            {decision.label}
          </button>
        ))}
      </div>
      <ul className="decision-hints">
        <li>
          <strong>TRACK</strong>
          Mark it. Stay sharp.
        </li>
        <li>
          <strong>I TOOK THIS</strong>
          You took the risk. Journal it.
        </li>
        <li>
          <strong>WATCH ONLY</strong>
          Eyes on. No claim of a fill.
        </li>
        <li>
          <strong>NO TRADE</strong>
          Passing is Pack strength.
        </li>
      </ul>
      <p className="fine">{note ?? "The seal stays on this device. It is not an order."}</p>
      <p className="fine">Pack XP here is a cosmetic preview, not a ledger entry.</p>
    </section>
  );
}
