from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_user, db_session, limit, now_utc, prepare, raise_action
from app.db.models import Journal, Mission, ReplayAttempt, ReplayChallenge, User, UserMissionDecision, XpLedger
from app.domain.board import normalize_prefs
from app.domain.progression import rank_name
from app.services.actions import REPLAY_DISCLAIMER, ActionError, concealed_replay, lock_decision, record_replay, submit_journal

router = APIRouter()


class LockBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision: str = Field(pattern="^(TRACK|I_TOOK_THIS|WATCH_ONLY|NO_TRADE)$")


class JournalBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    process_notes: str = Field(default="", max_length=4000)
    emotional_state: str = Field(default="", max_length=64)
    followed_plan: str = Field(default="", max_length=4000)
    self_reported_result: str | None = Field(default=None, max_length=32)
    reason: str | None = Field(default=None, max_length=4000)
    lesson: str | None = Field(default=None, max_length=4000)


class ReplayBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chosen_tier: str = Field(pattern="^(HUNT|STANDARD)$")
    chosen_decision: str = Field(pattern="^(TRACK|TAKE|WATCH|NO_TRADE)$")
    evidence_reviewed: bool = False
    idempotency_key: str = Field(min_length=1, max_length=120)


def _sync(request: Request, db: Session) -> None:
    prepare(request, db)


@router.get("/api/me")
def me(request: Request, user: User = Depends(current_user), db: Session = Depends(db_session)) -> dict:
    _sync(request, db)
    total = int(
        db.scalar(select(func.coalesce(func.sum(XpLedger.amount), 0)).where(XpLedger.user_id == user.id)) or 0
    )
    user.pack_xp = total
    user.wolf_rank = rank_name(total)
    rows = db.execute(
        select(Mission.cci_setup_id, UserMissionDecision.decision)
        .join(UserMissionDecision, UserMissionDecision.mission_id == Mission.id)
        .where(UserMissionDecision.user_id == user.id)
    ).all()
    decisions = {setup_id: decision for setup_id, decision in rows}
    journals = db.scalars(
        select(Mission.cci_setup_id)
        .join(Journal, Journal.mission_id == Mission.id)
        .where(Journal.user_id == user.id)
    ).all()
    replay_count = int(
        db.scalar(select(func.count(ReplayAttempt.id)).where(ReplayAttempt.user_id == user.id)) or 0
    )
    return {
        "display_name": user.display_name,
        "telegram_user_id": user.telegram_user_id,
        "pack_xp": total,
        "wolf_rank": rank_name(total),
        "discipline_streak": int(user.discipline_streak or 0),
        "decisions": decisions,
        "journal_ids": list(journals),
        "replay_count": replay_count,
        "no_trade_count": sum(1 for decision in decisions.values() if decision == "NO_TRADE"),
        "notification_prefs": normalize_prefs(user.notification_prefs),
        "oath_accepted": user.oath_accepted_at is not None,
    }


@router.get("/api/missions/{cci_setup_id}/decision")
def read_decision(
    cci_setup_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _sync(request, db)
    row = db.execute(
        select(UserMissionDecision.decision)
        .join(Mission, Mission.id == UserMissionDecision.mission_id)
        .where(UserMissionDecision.user_id == user.id, Mission.cci_setup_id == cci_setup_id)
    ).first()
    return {"decision": None if row is None else row[0]}


@router.post("/api/missions/{cci_setup_id}/decision")
def post_decision(
    cci_setup_id: str,
    body: LockBody,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> JSONResponse:
    _sync(request, db)
    limit(f"lock:{user.id}", 60, 60)
    try:
        result = lock_decision(db, user, cci_setup_id, body.decision, now_utc())
    except ActionError as exc:
        raise_action(exc)
    status = 409 if result["conflict"] else 200
    return JSONResponse(status_code=status, content=result)


@router.get("/api/missions/{cci_setup_id}/journal")
def read_journal(
    cci_setup_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _sync(request, db)
    row = db.scalar(
        select(Journal)
        .join(Mission, Mission.id == Journal.mission_id)
        .where(Journal.user_id == user.id, Mission.cci_setup_id == cci_setup_id)
    )
    if row is None:
        return {"journal": None}
    from app.services.actions import _journal_payload

    return {"journal": _journal_payload(row)}


@router.post("/api/missions/{cci_setup_id}/journal")
def post_journal(
    cci_setup_id: str,
    body: JournalBody,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _sync(request, db)
    limit(f"journal:{user.id}", 20, 3600)
    try:
        return submit_journal(db, user, cci_setup_id, body.model_dump(), now_utc())
    except ActionError as exc:
        raise_action(exc)


def _attempted(db: Session, user: User, challenge_id) -> bool:
    return (
        db.scalar(
            select(ReplayAttempt.id).where(
                ReplayAttempt.user_id == user.id,
                ReplayAttempt.challenge_id == challenge_id,
            )
        )
        is not None
    )


@router.get("/api/replay")
def list_replay(request: Request, user: User = Depends(current_user), db: Session = Depends(db_session)) -> dict:
    _sync(request, db)
    limit(f"replay-read:{user.id}", 120, 60)
    rows = db.execute(
        select(Mission, ReplayChallenge).join(ReplayChallenge, ReplayChallenge.source_mission_id == Mission.id).where(
            ReplayChallenge.active.is_(True)
        )
    ).all()
    tapes = [
        concealed_replay(mission, challenge, _attempted(db, user, challenge.id))
        for mission, challenge in rows
    ]
    return {"disclaimer": REPLAY_DISCLAIMER, "tapes": tapes}


@router.get("/api/missions/{cci_setup_id}/replay")
def read_replay(
    cci_setup_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _sync(request, db)
    limit(f"replay-read:{user.id}", 120, 60)
    mission = db.scalar(select(Mission).where(Mission.cci_setup_id == cci_setup_id))
    if mission is None:
        raise HTTPException(status_code=404, detail="This mission is not in the fixture set.")
    challenge = db.scalar(select(ReplayChallenge).where(ReplayChallenge.source_mission_id == mission.id))
    if challenge is None or not challenge.active:
        raise HTTPException(status_code=404, detail="This fixture has no replay brief.")
    return concealed_replay(mission, challenge, _attempted(db, user, challenge.id))


@router.post("/api/missions/{cci_setup_id}/replay")
def post_replay(
    cci_setup_id: str,
    body: ReplayBody,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _sync(request, db)
    limit(f"replay:{user.id}", 10, 3600)
    if not body.idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Replay needs an idempotency key.")
    try:
        return record_replay(
            db,
            user,
            cci_setup_id,
            chosen_tier=body.chosen_tier,
            chosen_decision=body.chosen_decision,
            evidence_reviewed=body.evidence_reviewed,
            idempotency_key=body.idempotency_key.strip(),
            now=now_utc(),
        )
    except ActionError as exc:
        raise_action(exc)
