import { useState } from "react";
import {
  DECISIONS,
  decisionLabel,
  lockDecision,
  useDecisions,
  type DecisionId,
} from "../decisions/localDecisions";
import { telegramBridge } from "../telegram/TelegramBridge";

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
        ? `Locked on this device · ${decisionLabel(stored)}`
        : `Already locked · ${decisionLabel(stored)}`,
    );
  }

  return (
    <section className="decision-panel" aria-label="Decision lock">
      <div className="panel-head">
        <p className="kicker">Decision</p>
        <p className="fine">{locked ? `Locked · ${decisionLabel(locked)}` : "Unlocked"}</p>
      </div>
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
      <p className="fine">{note ?? "Preview lock stays on this device. It is not an order."}</p>
      <p className="fine">Pack XP is not awarded in this preview.</p>
    </section>
  );
}
