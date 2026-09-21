import { useEffect, useState } from "react";
import { invalidatePackProfile } from "../api/profile";
import { apiFetch } from "../api/server";
import {
  DECISIONS,
  decisionLabel,
  rememberDecision,
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

  useEffect(() => {
    let cancel = false;
    apiFetch(`/api/missions/${encodeURIComponent(missionId)}/decision`)
      .then(async (response) => (response.ok ? response.json() : null))
      .then((body: { decision?: DecisionId | null } | null) => {
        if (cancel || !body?.decision) return;
        rememberDecision(missionId, body.decision);
      })
      .catch(() => undefined);
    return () => {
      cancel = true;
    };
  }, [missionId]);

  async function choose(decision: DecisionId) {
    if (locked) return;
    const response = await apiFetch(`/api/missions/${encodeURIComponent(missionId)}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    });
    if (!response.ok && response.status !== 409) {
      setNote("The seal did not land. Nothing was stored.");
      return;
    }
    const body = (await response.json()) as { decision?: DecisionId };
    const stored = body.decision ? rememberDecision(missionId, body.decision) : decision;
    telegramBridge.impact("medium");
    setNote(
      stored === decision ? `${SEAL_LINE[stored]} Sealed.` : `Already sealed · ${decisionLabel(stored)}`,
    );
    invalidatePackProfile();
  }

  return (
    <section className="decision-panel" aria-label="Decision lock">
      <div className="panel-head">
        <p className="kicker">Your call</p>
        <p className="fine">{locked ? "Sealed" : "Once"}</p>
      </div>
      {locked ? (
        <div className="lock-seal" data-testid="decision-lock">
          <p className="kicker">Sealed</p>
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
      <p className="fine">{note ?? "The seal stays. It is not an order."}</p>
      <p className="fine">Pack XP lands in the server ledger. A retry cannot add more.</p>
    </section>
  );
}
