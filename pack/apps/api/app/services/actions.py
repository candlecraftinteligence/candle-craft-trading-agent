from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Journal,
    Mission,
    MissionLifecycleEvent,
    ProcessMark,
    ReplayAttempt,
    ReplayChallenge,
    User,
    UserMissionDecision,
)
from app.domain.progression import DECISION_XP, JOURNAL_XP, replay_base_xp

REPLAY_DISCLAIMER = (
    "Replay scores measure pattern recognition practice on closed historical setups. "
    "They do not predict future results. Past CCI setups do not guarantee future performance. "
    "The Pack never executes trades."
)
from app.services.ledger import award, touch_streak, used_today


class ActionError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


def _mission(session: Session, cci_setup_id: str) -> Mission:
    mission = session.scalar(select(Mission).where(Mission.cci_setup_id == cci_setup_id))
    if mission is None:
        raise ActionError(404, "This mission is not in the fixture set.")
    return mission


def _decision_row(session: Session, user: User, mission: Mission) -> UserMissionDecision | None:
    return session.scalar(
        select(UserMissionDecision).where(
            UserMissionDecision.user_id == user.id,
            UserMissionDecision.mission_id == mission.id,
        )
    )


def lock_decision(session: Session, user: User, cci_setup_id: str, decision: str, now: datetime) -> dict:
    if decision not in DECISION_XP:
        raise ActionError(422, "Decision must be TRACK, I TOOK THIS, WATCH ONLY, or NO TRADE.")
    mission = _mission(session, cci_setup_id)
    existing = _decision_row(session, user, mission)
    if existing is not None:
        return {
            "decision": existing.decision,
            "created": False,
            "xp_awarded": 0,
            "conflict": existing.decision != decision,
        }
    row = UserMissionDecision(user_id=user.id, mission_id=mission.id, decision=decision, locked_at=now)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = _decision_row(session, user, mission)
        if existing is None:
            raise
        return {
            "decision": existing.decision,
            "created": False,
            "xp_awarded": 0,
            "conflict": existing.decision != decision,
        }
    granted = award(
        session,
        user,
        raw_amount=DECISION_XP[decision],
        category="decision",
        reason_code=f"lock:{decision}",
        idempotency_key=f"decision:{user.id}:{mission.id}",
        now=now,
        ref_type="user_mission_decision",
        ref_id=str(row.id),
    )
    touch_streak(session, user, now)
    return {"decision": decision, "created": True, "xp_awarded": granted, "conflict": False}


def submit_journal(session: Session, user: User, cci_setup_id: str, fields: dict, now: datetime) -> dict:
    mission = _mission(session, cci_setup_id)
    existing = session.scalar(
        select(Journal).where(Journal.user_id == user.id, Journal.mission_id == mission.id)
    )
    if existing is not None:
        return {"journal": _journal_payload(existing), "created": False, "xp_awarded": 0}

    note = str(fields.get("process_notes") or "").strip()
    confidence = str(fields.get("emotional_state") or "").strip()
    plan = str(fields.get("followed_plan") or "").strip()
    reason = str(fields.get("reason") or "").strip()
    lesson = str(fields.get("lesson") or "").strip()
    result = fields.get("self_reported_result")
    if not note or not confidence or not plan:
        raise ActionError(422, "Note, confidence, and risk-plan note are required.")
    locked = _decision_row(session, user, mission)
    if locked is not None and locked.decision == "I_TOOK_THIS" and not reason:
        raise ActionError(422, "I TOOK THIS needs a reason. It is still self-reported.")
    resolved = mission.resolved_at is not None or mission.outcome_code is not None
    if resolved and (not result or not lesson):
        raise ActionError(422, "After resolution, add a self-reported result and a lesson.")
    if result is not None and result not in {"WIN", "LOSS", "BREAKEVEN", "PARTIAL", "DID_NOT_ENTER"}:
        raise ActionError(422, "Self-reported result is not one of the journal values.")

    row = Journal(
        user_id=user.id,
        mission_id=mission.id,
        emotional_state=confidence,
        process_notes=note,
        followed_plan=plan,
        self_reported_result=result if resolved else None,
        reason=reason or None,
        lesson=lesson or None,
        screenshot_url=None,
        created_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(Journal).where(Journal.user_id == user.id, Journal.mission_id == mission.id)
        )
        if existing is None:
            raise
        return {"journal": _journal_payload(existing), "created": False, "xp_awarded": 0}

    granted = 0
    if resolved:
        granted = award(
            session,
            user,
            raw_amount=JOURNAL_XP,
            category="journal_review",
            reason_code="journal",
            idempotency_key=f"journal:{user.id}:{mission.id}",
            now=now,
            ref_type="journal",
            ref_id=str(row.id),
        )
        touch_streak(session, user, now)
    return {"journal": _journal_payload(row), "created": True, "xp_awarded": granted}


def _journal_payload(row: Journal) -> dict:
    return {
        "process_notes": row.process_notes,
        "emotional_state": row.emotional_state,
        "followed_plan": row.followed_plan,
        "self_reported_result": row.self_reported_result,
        "reason": row.reason,
        "lesson": row.lesson,
        "user_reported": True,
    }


def concealed_replay(mission: Mission, challenge: ReplayChallenge, attempted: bool) -> dict:
    """Study payload. Outcome, symbol, and the teaching note stay out until reveal."""
    fixture = challenge.fixture_json or {}
    return {
        "cci_setup_id": mission.cci_setup_id,
        "timeframe": mission.timeframe,
        "direction": mission.direction,
        "masked_title": fixture.get("masked_title") or "",
        "masked_thesis": fixture.get("masked_thesis") or "",
        "evidence": fixture.get("evidence") or [],
        "attempted": attempted,
        "disclaimer": REPLAY_DISCLAIMER,
        "concealed": True,
    }


def _score_parts(*, actual_tier: str, chosen_tier: str, preferred: str, chosen: str, evidence_reviewed: bool) -> dict:
    quality = 40 if actual_tier == chosen_tier else 0
    decision = 40 if preferred == chosen else 0
    attention = 20 if evidence_reviewed else 0
    return {"quality": quality, "decision_points": decision, "attention": attention, "score": quality + decision + attention}


def _reveal_payload(mission: Mission, challenge: ReplayChallenge, session: Session, score: int, parts: dict) -> dict:
    events = session.scalars(
        select(MissionLifecycleEvent)
        .where(MissionLifecycleEvent.mission_id == mission.id)
        .order_by(MissionLifecycleEvent.occurred_at)
    ).all()
    return {
        "concealed": False,
        "disclaimer": REPLAY_DISCLAIMER,
        "symbol": mission.symbol,
        "outcome_code": mission.outcome_code,
        "teaching_note": challenge.teaching_note,
        "quality_tier": mission.quality_tier,
        "score": score,
        "quality": parts["quality"],
        "decision_points": parts["decision_points"],
        "attention": parts["attention"],
        "lifecycle": [
            {
                "cci_event_id": event.cci_event_id,
                "event_type": event.event_type,
                "state": event.state,
                "occurred_at": event.occurred_at.isoformat(),
                "outcome_code": None if not event.payload_json else event.payload_json.get("outcome_code"),
            }
            for event in events
        ],
    }


def record_replay(
    session: Session,
    user: User,
    cci_setup_id: str,
    *,
    chosen_tier: str,
    chosen_decision: str,
    evidence_reviewed: bool,
    idempotency_key: str,
    now: datetime,
) -> dict:
    mission = _mission(session, cci_setup_id)
    challenge = session.scalar(select(ReplayChallenge).where(ReplayChallenge.source_mission_id == mission.id))
    if challenge is None or not challenge.active:
        raise ActionError(404, "This fixture has no replay brief.")
    if chosen_tier not in {"HUNT", "STANDARD"}:
        raise ActionError(422, "Name the tier as HUNT or STANDARD.")
    if chosen_decision not in {"TRACK", "TAKE", "WATCH", "NO_TRADE"}:
        raise ActionError(422, "Training decision is not in the drill set.")
    key = f"replay:{user.id}:{idempotency_key}"
    existing = session.scalar(select(ReplayAttempt).where(ReplayAttempt.idempotency_key == key))
    if existing is not None:
        rubric = challenge.rubric_json or {}
        detail = existing.detail_json or {}
        parts = _score_parts(
            actual_tier=str(rubric.get("quality_tier") or ""),
            chosen_tier=str(detail.get("chosen_tier") or ""),
            preferred=str(rubric.get("preferred_decision") or ""),
            chosen=existing.decision,
            evidence_reviewed=bool(detail.get("evidence_reviewed")),
        )
        parts["score"] = existing.score
        return {
            "score": existing.score,
            "created": False,
            "xp_awarded": 0,
            "decision": existing.decision,
            **_reveal_payload(mission, challenge, session, existing.score, parts),
        }

    start = datetime.combine(now.astimezone(timezone.utc).date(), time.min, tzinfo=timezone.utc)
    attempts_before = int(
        session.scalar(
            select(func.count(ReplayAttempt.id)).where(
                ReplayAttempt.user_id == user.id,
                ReplayAttempt.created_at >= start,
                ReplayAttempt.created_at < start + timedelta(days=1),
            )
        )
        or 0
    )
    rubric = challenge.rubric_json or {}
    parts = _score_parts(
        actual_tier=str(rubric.get("quality_tier") or ""),
        chosen_tier=chosen_tier,
        preferred=str(rubric.get("preferred_decision") or ""),
        chosen=chosen_decision,
        evidence_reviewed=evidence_reviewed,
    )
    score = parts["score"]
    raw = replay_base_xp(attempts_before, score)
    attempt = ReplayAttempt(
        user_id=user.id,
        challenge_id=challenge.id,
        decision=chosen_decision,
        score=score,
        detail_json={"chosen_tier": chosen_tier, "evidence_reviewed": evidence_reviewed},
        idempotency_key=key,
        created_at=now,
    )
    session.add(attempt)
    session.flush()
    granted = award(
        session,
        user,
        raw_amount=raw,
        category="replay",
        reason_code="replay",
        idempotency_key=key,
        now=now,
        ref_type="replay_attempt",
        ref_id=str(attempt.id),
    )
    touch_streak(session, user, now)
    return {
        "score": score,
        "created": True,
        "xp_awarded": granted,
        "decision": chosen_decision,
        **_reveal_payload(mission, challenge, session, score, parts),
    }


def category_used(session: Session, user: User, category: str, now: datetime) -> int:
    return used_today(session, user.id, category, now)


def record_mark(session: Session, user: User, cci_setup_id: str, kind: str, now: datetime) -> dict:
    if kind not in {"evidence", "review"}:
        raise ActionError(422, "Mark evidence or a review. Nothing else counts.")
    mission = _mission(session, cci_setup_id)
    if kind == "review" and mission.resolved_at is None and mission.outcome_code is None:
        raise ActionError(422, "A review opens after the fixture resolves.")
    existing = session.scalar(
        select(ProcessMark).where(
            ProcessMark.user_id == user.id,
            ProcessMark.mission_id == mission.id,
            ProcessMark.kind == kind,
        )
    )
    if existing is not None:
        return {"kind": kind, "created": False, "xp_awarded": 0}
    row = ProcessMark(user_id=user.id, mission_id=mission.id, kind=kind, created_at=now)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return {"kind": kind, "created": False, "xp_awarded": 0}
    raw = 5 if kind == "evidence" else 20
    category = "process" if kind == "evidence" else "journal_review"
    granted = award(
        session,
        user,
        raw_amount=raw,
        category=category,
        reason_code=kind,
        idempotency_key=f"{kind}:{user.id}:{mission.id}",
        now=now,
        ref_type="process_mark",
        ref_id=str(row.id),
    )
    touch_streak(session, user, now)
    return {"kind": kind, "created": True, "xp_awarded": granted}
