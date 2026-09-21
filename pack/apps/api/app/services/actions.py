from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Journal, Mission, ReplayAttempt, ReplayChallenge, User, UserMissionDecision
from app.domain.progression import DECISION_XP, JOURNAL_XP, replay_base_xp, score_replay
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
        return {"score": existing.score, "created": False, "xp_awarded": 0, "decision": existing.decision}

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
    score = score_replay(
        actual_tier=str(rubric.get("quality_tier") or ""),
        chosen_tier=chosen_tier,
        preferred=str(rubric.get("preferred_decision") or ""),
        chosen=chosen_decision,
        evidence_reviewed=evidence_reviewed,
    )
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
    return {"score": score, "created": True, "xp_awarded": granted, "decision": chosen_decision}


def category_used(session: Session, user: User, category: str, now: datetime) -> int:
    return used_today(session, user.id, category, now)
