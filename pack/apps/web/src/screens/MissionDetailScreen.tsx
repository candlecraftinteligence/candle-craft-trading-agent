import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { fetchMission, MissionNotFoundError } from "../api/missions";
import type { Mission } from "../api/types";
import { DecisionPanel } from "../components/DecisionPanel";
import { LifecycleTimeline } from "../components/LifecycleTimeline";
import { OUTCOME_SEPARATION, RISK_WARNING } from "../copy";
import { toneForState } from "../presentation";
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
        Missions
      </Link>
      {load.status === "loading" ? <p className="status-line">Reading fixture…</p> : null}
      {load.status === "missing" ? (
        <p className="status-line">This mission is not in the fixture set.</p>
      ) : null}
      {load.status === "error" ? (
        <p className="status-line">{load.message} Nothing was invented in its place.</p>
      ) : null}
      {load.status === "ready" ? <Detail mission={load.mission} /> : null}
    </div>
  );
}

function Detail({ mission }: { mission: Mission }) {
  return (
    <>
      <header>
        <div className="detail-symbol-row">
          <span className="symbol">{mission.symbol}</span>
          {mission.quality_tier === "HUNT" ? (
            <span className="hunt-badge">Hunt</span>
          ) : (
            <span className="tier-standard">{mission.quality_tier}</span>
          )}
        </div>
        <div className="meta-row">
          <span>{mission.timeframe}</span>
          <span>{mission.direction}</span>
          <span className="state-chip" data-tone={toneForState(mission.lifecycle_state)}>
            {mission.lifecycle_state}
          </span>
        </div>
        <h1 className="detail-title">{mission.title}</h1>
      </header>

      <section className="panel">
        <p className="kicker">Thesis</p>
        <p className="body-copy">{mission.thesis_summary}</p>
        <p className="fine">{RISK_WARNING}</p>
      </section>

      <section className="panel">
        <p className="kicker">Evidence</p>
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
      </section>

      <DecisionPanel missionId={mission.cci_setup_id} />

      <section className="panel">
        <p className="kicker">Lifecycle</p>
        <p className="fine">Events below are the fixture record, in order.</p>
        <LifecycleTimeline events={mission.lifecycle} />
      </section>

      {mission.outcome_code ? (
        <section className="panel outcome-block">
          <p className="kicker">CCI Outcome</p>
          <p className="outcome-code">{mission.outcome_code}</p>
          <p className="fine">{OUTCOME_SEPARATION}</p>
        </section>
      ) : null}

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
