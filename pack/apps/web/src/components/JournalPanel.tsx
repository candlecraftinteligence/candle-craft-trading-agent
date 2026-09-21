import { useState } from "react";
import type { DecisionId } from "../decisions/localDecisions";
import {
  CONFIDENCE_LEVELS,
  JOURNAL_RESULTS,
  saveJournal,
  useJournals,
  type ConfidenceLevel,
  type JournalResult,
} from "../storage/journals";

type JournalPanelProps = {
  missionId: string;
  resolved: boolean;
  decision: DecisionId | null;
};

export function JournalPanel({ missionId, resolved, decision }: JournalPanelProps) {
  const saved = useJournals()[missionId] ?? null;
  const [note, setNote] = useState(saved?.note ?? "");
  const [confidence, setConfidence] = useState<ConfidenceLevel | null>(saved?.confidence ?? null);
  const [riskPlanNote, setRiskPlanNote] = useState(saved?.riskPlanNote ?? "");
  const [reason, setReason] = useState(saved?.reason ?? "");
  const [result, setResult] = useState<JournalResult | null>(saved?.result ?? null);
  const [lesson, setLesson] = useState(saved?.lesson ?? "");
  const [error, setError] = useState<string | null>(null);
  const needsReason = decision === "I_TOOK_THIS";

  function submit() {
    if (saved) return;
    if (!note.trim() || !confidence || !riskPlanNote.trim()) {
      setError("Note, confidence, and risk-plan note are required.");
      return;
    }
    if (needsReason && !reason.trim()) {
      setError("I TOOK THIS needs a reason. It is still self-reported.");
      return;
    }
    if (resolved && (!result || !lesson.trim())) {
      setError("After resolution, add a self-reported result and a lesson.");
      return;
    }
    saveJournal({
      missionId,
      note: note.trim(),
      confidence,
      riskPlanNote: riskPlanNote.trim(),
      reason: needsReason ? reason.trim() : null,
      result: resolved ? result : null,
      lesson: resolved ? lesson.trim() : null,
    });
    setError(null);
  }

  return (
    <section className="panel journal-block" id="journal" data-testid="user-journal" aria-label="Your Journal">
      <div className="panel-head">
        <p className="kicker">Your Journal</p>
        <p className="user-reported">USER-REPORTED</p>
      </div>
      <p className="fine">
        Your notes, filed by you. They do not edit the CCI outcome and they are not proof of PnL.
      </p>
      {saved ? (
        <dl className="journal-readout">
          <div>
            <dt>Note</dt>
            <dd>{saved.note}</dd>
          </div>
          <div>
            <dt>Confidence</dt>
            <dd>{saved.confidence}</dd>
          </div>
          <div>
            <dt>Risk-plan note</dt>
            <dd>{saved.riskPlanNote}</dd>
          </div>
          {saved.reason ? (
            <div>
              <dt>Reason</dt>
              <dd>{saved.reason}</dd>
            </div>
          ) : null}
          {saved.result ? (
            <div>
              <dt>Self-reported result</dt>
              <dd>{saved.result}</dd>
            </div>
          ) : null}
          {saved.lesson ? (
            <div>
              <dt>Lesson</dt>
              <dd>{saved.lesson}</dd>
            </div>
          ) : null}
        </dl>
      ) : (
        <form
          className="journal-form"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <label className="field">
            <span>Note</span>
            <textarea value={note} onChange={(event) => setNote(event.target.value)} rows={3} />
          </label>
          <fieldset className="choice-set">
            <legend>Confidence</legend>
            {CONFIDENCE_LEVELS.map((level) => (
              <label key={level}>
                <input
                  type="radio"
                  name={`confidence-${missionId}`}
                  checked={confidence === level}
                  onChange={() => setConfidence(level)}
                />
                {level}
              </label>
            ))}
          </fieldset>
          <label className="field">
            <span>Risk-plan note</span>
            <textarea value={riskPlanNote} onChange={(event) => setRiskPlanNote(event.target.value)} rows={2} />
          </label>
          {needsReason ? (
            <label className="field">
              <span>Reason</span>
              <textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={2} />
            </label>
          ) : null}
          {resolved ? (
            <>
              <fieldset className="choice-set">
                <legend>Self-reported result</legend>
                {JOURNAL_RESULTS.map((item) => (
                  <label key={item}>
                    <input
                      type="radio"
                      name={`result-${missionId}`}
                      checked={result === item}
                      onChange={() => setResult(item)}
                    />
                    {item === "DID_NOT_ENTER" ? "DID NOT ENTER" : item}
                  </label>
                ))}
              </fieldset>
              <label className="field">
                <span>Lesson</span>
                <textarea value={lesson} onChange={(event) => setLesson(event.target.value)} rows={2} />
              </label>
            </>
          ) : (
            <p className="fine">Result and lesson open after the fixture resolves. They stay user-reported.</p>
          )}
          {error ? <p className="fine">{error}</p> : null}
          <button type="submit" className="btn primary">
            Save journal
          </button>
        </form>
      )}
    </section>
  );
}
