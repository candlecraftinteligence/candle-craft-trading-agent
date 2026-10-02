import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { fetchMission, MissionNotFoundError } from "../api/missions";
import { invalidateQuests } from "../api/quests";
import { apiFetch } from "../api/server";
import type { Mission } from "../api/types";
import { DecisionPanel } from "../components/DecisionPanel";
import { IllustrativeChart } from "../components/IllustrativeChart";
import { JournalPanel } from "../components/JournalPanel";
import { LifecycleTimeline } from "../components/LifecycleTimeline";
import { QuestChip } from "../components/QuestChip";
import { XpPreview } from "../components/XpPreview";
import { OUTCOME_SEPARATION, RISK_WARNING } from "../copy";
import { useDecisions } from "../decisions/localDecisions";
import { missionPreview } from "../domain/xp";
import { toneForState } from "../presentation";
import { useJournals } from "../storage/journals";
import { markEvidenceRead, useMarks } from "../storage/marks";
import { telegramBridge } from "../telegram/TelegramBridge";

type LoadState =
  | { status: "loading" }
  | { status: "ready"; mission: Mission }
  | { status: "missing" }
  | { status: "error"; message: string };

export function MissionDetailScreen() {
  const { id } = useParams();
  const navigate = useNavigate();
  const load = useMission(id);

  useEffect(() => telegramBridge.bindBack(() => navigate("/missions")), [navigate]);

  return (
    <div className="stack">
      <Link className="back-link" to="/missions">
        The board
      </Link>
      {load.status === "loading" ? <p className="status-line">Reading fixture…</p> : null}
      {load.status === "missing" ? (
        <p className="status-line" data-testid="mission-missing">
          This mission is not in the fixture set.
        </p>
      ) : null}
      {load.status === "error" ? (
        <p className="status-line">{load.message} Nothing was invented in its place.</p>
      ) : null}
      {load.status === "ready" ? (
        <div className="detail-stack">
          <MissionDetailBody mission={load.mission} />
        </div>
      ) : null}
    </div>
  );
}

export function MissionDetailBody({ mission }: { mission: Mission }) {
  const decisions = useDecisions();
  const journals = useJournals();
  const marks = useMarks();
  const decision = decisions[mission.cci_setup_id] ?? null;
  const evidenceRead = marks.evidenceRead.includes(mission.cci_setup_id);
  const journalSaved = Boolean(journals[mission.cci_setup_id]);
  const reviewSaved = marks.reviews.includes(mission.cci_setup_id);
  const preview = missionPreview({
    evidenceRead,
    decision,
    journalSaved,
    resolved: mission.resolved,
    reviewSaved,
  });
  const tone = toneForState(mission.lifecycle_state);

  const invalidation = mission.evidence.find((block) => block.type === "invalidation");
  const target = mission.evidence.find(
    (block) => /target/i.test(block.type) || /target/i.test(block.label),
  );

  return (
    <>
      <header className="detail-hero">
        <img src="/brand/wolf-detail.webp" alt="" />
        <div>
          <p className="kicker">Candle Craft Intelligence</p>
          <div className="detail-symbol-row">
            <span className="symbol">{mission.symbol}</span>
          </div>
          <div className="meta-row">
            <span>{mission.timeframe}</span>
            <span>{mission.direction}</span>
            <span className="state-chip" data-tone={tone}>
              {mission.lifecycle_state}
            </span>
            {mission.quality_tier === "HUNT" ? (
              <span className="hunt-badge">Hunt</span>
            ) : (
              <span className="tier-standard">{mission.quality_tier}</span>
            )}
          </div>
        </div>
      </header>

      <h1 className="detail-title">{mission.title}</h1>
      <p className="fine">
        Synthetic {mission.timeframe} {mission.direction.toLowerCase()} setup
      </p>

      <IllustrativeChart mission={mission} />

      <section className="panel">
        <p className="kicker">The idea</p>
        <p className="body-copy">{mission.thesis_summary}</p>
        <p className="fine">{RISK_WARNING}</p>
      </section>

      <section className="panel" id="evidence">
        <p className="kicker">What the tape shows</p>
        {mission.evidence.length === 0 ? (
          <p className="status-line">No evidence blocks were included in this fixture.</p>
        ) : (
          <ul className="evidence-list">
            {mission.evidence.map((block) => (
              <li key={`${block.type}-${block.label}`} className="evidence-item">
                <div className="evidence-type">{block.type}</div>
                <p className="section-title">{block.label}</p>
                {block.detail ? <p className="body-copy">{block.detail}</p> : null}
              </li>
            ))}
          </ul>
        )}
        <button
          type="button"
          className="btn"
          disabled={evidenceRead}
          onClick={() => {
            void apiFetch(`/api/missions/${encodeURIComponent(mission.cci_setup_id)}/mark`, {
              method: "POST",
              body: JSON.stringify({ kind: "evidence" }),
            }).then((response) => {
              if (!response.ok) return;
              markEvidenceRead(mission.cci_setup_id);
              invalidateQuests();
            });
          }}
        >
          {evidenceRead ? "Evidence read" : "I've read the tape"}
        </button>
      </section>

      <DecisionPanel missionId={mission.cci_setup_id} />

      <section className="panel intel-row" aria-label="Pack intel">
        <div className="panel-head">
          <p className="kicker">Pack intel</p>
          <p className="fine">Fixture fields only</p>
        </div>
        <ul className="intel-grid">
          <li>
            <span>Key level</span>
            <strong>—</strong>
          </li>
          <li>
            <span>Bias</span>
            <strong>{mission.direction}</strong>
          </li>
          <li>
            <span>Invalidation</span>
            <strong>{invalidation?.label ?? "—"}</strong>
          </li>
          <li>
            <span>Targets</span>
            <strong>{target?.label ?? "—"}</strong>
          </li>
        </ul>
        <p className="fine">A dash means the fixture did not record that number.</p>
      </section>

      <section className="panel">
        <p className="kicker">The tape</p>
        <p className="fine">Fixture events only, in the order they were written.</p>
        <LifecycleTimeline events={mission.lifecycle} />
      </section>

      {mission.resolved ? (
        <section className="panel outcome-block" data-testid="cci-outcome">
          <p className="kicker">CCI Outcome</p>
          <p className="outcome-code">{mission.outcome_code ?? "No outcome code in this fixture."}</p>
          <p className="fine">{OUTCOME_SEPARATION}</p>
        </section>
      ) : null}

      <JournalPanel missionId={mission.cci_setup_id} resolved={mission.resolved} decision={decision} />

      <XpPreview lines={preview.lines} total={preview.total} />

      <QuestChip
        missionId={mission.cci_setup_id}
        resolved={mission.resolved}
        decision={decision}
        evidenceRead={evidenceRead}
        journalSaved={journalSaved}
        reviewSaved={reviewSaved}
      />

      <p className="fine">{mission.disclaimer}</p>
    </>
  );
}

function useMission(id: string | undefined): LoadState {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    if (!id) {
      setState({ status: "missing" });
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    fetchMission(id)
      .then((mission) => {
        if (!cancelled) setState({ status: "ready", mission });
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        if (reason instanceof MissionNotFoundError) {
          setState({ status: "missing" });
          return;
        }
        setState({
          status: "error",
          message: reason instanceof Error ? reason.message : "Mission unavailable.",
        });
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  return state;
}
