import { Link } from "react-router-dom";
import { invalidateQuests } from "../api/quests";
import { apiFetch } from "../api/server";
import type { DecisionId } from "../decisions/localDecisions";
import { markReview } from "../storage/marks";

type QuestChipProps = {
  missionId: string;
  resolved: boolean;
  decision: DecisionId | null;
  evidenceRead: boolean;
  journalSaved: boolean;
  reviewSaved: boolean;
};

export function QuestChip({
  missionId,
  resolved,
  decision,
  evidenceRead,
  journalSaved,
  reviewSaved,
}: QuestChipProps) {
  const step = nextStep({ resolved, decision, evidenceRead, journalSaved, reviewSaved });
  return (
    <section className="panel quest-chip" aria-label="Quest">
      <p className="kicker">Drill</p>
      <p className="section-title">{step.title}</p>
      <p className="fine">{step.detail}</p>
      {step.kind === "review" ? (
        <button
          type="button"
          className="btn primary"
          onClick={() => {
            void apiFetch(`/api/missions/${encodeURIComponent(missionId)}/mark`, {
              method: "POST",
              body: JSON.stringify({ kind: "review" }),
            }).then((response) => {
              if (!response.ok) return;
              markReview(missionId);
              invalidateQuests();
            });
          }}
        >
          Mark the review done
        </button>
      ) : null}
      {step.kind === "replay" ? (
        <Link className="btn primary" to="/replay">
          Run the tape
        </Link>
      ) : null}
      {step.kind === "journal" ? (
        <a className="btn primary" href="#journal">
          Open the den journal
        </a>
      ) : null}
    </section>
  );
}

function nextStep(input: Omit<QuestChipProps, "missionId">): {
  title: string;
  detail: string;
  kind: "evidence" | "lock" | "journal" | "review" | "replay" | "done";
} {
  if (!input.evidenceRead) {
    return {
      title: "Read the evidence all the way",
      detail: "Mark the evidence read above. That is the drill.",
      kind: "evidence",
    };
  }
  if (!input.decision) {
    return {
      title: "Seal a call",
      detail: "The four choices above are the seal. A pass counts.",
      kind: "lock",
    };
  }
  if (input.resolved && !input.journalSaved) {
    return {
      title: "Write the den journal",
      detail: "Your notes stay in Your Journal. They stay separate from the CCI outcome.",
      kind: "journal",
    };
  }
  if (input.resolved && input.journalSaved && !input.reviewSaved) {
    return {
      title: "Read both records",
      detail: "CCI outcome and your journal stay side by side. They are not the same page.",
      kind: "review",
    };
  }
  if (input.decision === "NO_TRADE") {
    return {
      title: "Pass on purpose",
      detail: "NO TRADE is sealed on this device.",
      kind: "done",
    };
  }
  return {
    title: "Run one closed tape",
    detail: "When the den is quiet, train on a closed setup.",
    kind: "replay",
  };
}
