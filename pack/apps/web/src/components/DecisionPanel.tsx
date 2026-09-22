import { useEffect, useState } from "react";
import { invalidatePackProfile } from "../api/profile";
import { invalidateQuests } from "../api/quests";
import { apiFetch } from "../api/server";
import {
  DECISIONS,
  decisionLabel,
  rememberDecision,
  useDecisions,
  type DecisionId,
} from "../decisions/localDecisions";
import { telegramBridge } from "../telegram/TelegramBridge";

const HELPERS: Record<DecisionId, string> = {
  TRACK: "Mark it. Stay sharp.",
  I_TOOK_THIS: "You took the risk. Journal it.",
  WATCH_ONLY: "Eyes on. No claim of a fill.",
  NO_TRADE: "Passing is Pack strength.",
};

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
    setNote(stored === decision ? `${SEAL_LINE[stored]} Sealed.` : `Already sealed · ${decisionLabel(stored)}`);
    invalidatePackProfile();
    invalidateQuests();
  }

  return (
    <section className="decision-panel" aria-label="Decision lock">
      <div className="panel-head">
        <p className="kicker">Your call</p>
        <p className="fine">Discipline wins</p>
      </div>
      {locked ? (
        <div className="lock-seal" data-testid="decision-lock">
          <p className="kicker">Sealed</p>
          <p className="lock-choice">{decisionLabel(locked)}</p>
        </div>
      ) : null}
      <div className="decision-grid call-grid">
        {DECISIONS.map((decision) => (
          <button
            key={decision.id}
            type="button"
            className="decision-btn"
            aria-label={decision.label}
            aria-pressed={locked === decision.id}
            disabled={locked !== null}
            onClick={() => choose(decision.id)}
          >
            <DecisionIcon id={decision.id} />
            <span className="decision-copy">
              <span className="decision-name">{decision.label}</span>
              <span className="decision-help">{HELPERS[decision.id]}</span>
            </span>
          </button>
        ))}
      </div>
      <p className="seal-line">{note ?? "The seal stays. It is not an order."}</p>
      <p className="fine">Pack XP lands in the server ledger. A retry cannot add more.</p>
    </section>
  );
}

function DecisionIcon({ id }: { id: DecisionId }) {
  if (id === "TRACK") {
    return (
      <svg className="decision-icon" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="7" />
        <circle cx="12" cy="12" r="2" />
        <path d="M12 3v3M12 18v3M3 12h3M18 12h3" />
      </svg>
    );
  }
  if (id === "I_TOOK_THIS") {
    return (
      <svg className="decision-icon" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="8" />
        <path d="M8 12.5 11 15.5 16.5 9" />
      </svg>
    );
  }
  if (id === "WATCH_ONLY") {
    return (
      <svg className="decision-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M3 12s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6Z" />
        <circle cx="12" cy="12" r="2.2" />
      </svg>
    );
  }
  return (
    <svg className="decision-icon" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="8" />
      <path d="M9 9l6 6M15 9l-6 6" />
    </svg>
  );
}
