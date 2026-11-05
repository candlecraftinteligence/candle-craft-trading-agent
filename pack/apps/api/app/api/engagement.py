from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import current_user, db_session, limit, now_utc, prepare, raise_action
from app.db.models import User
from app.domain.board import DEFAULT_PREFS, normalize_prefs
from app.services.actions import ActionError, record_mark
from app.services.engine import settle_progression, unlocked_codes

router = APIRouter()


class PrefsBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    new_mission: bool | None = None
    lifecycle_resolution: bool | None = None
    quest_complete: bool | None = None
    streak: bool | None = None
    replay_nudge: bool | None = None


class MarkBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str


def _sync(request: Request, db: Session) -> None:
    prepare(request, db)


@router.get("/api/quests")
def quests(request: Request, user: User = Depends(current_user), db: Session = Depends(db_session)) -> dict:
    _sync(request, db)
    limit(f"quest:{user.id}", 60, 60)
    board = settle_progression(db, user, now_utc())
    return {"daily": board["daily"], "weekly": board["weekly"]}


@router.get("/api/achievements")
def achievements(request: Request, user: User = Depends(current_user), db: Session = Depends(db_session)) -> dict:
    _sync(request, db)
    settle_progression(db, user, now_utc())
    return {"unlocked": unlocked_codes(db, user)}


@router.post("/api/me/notification-prefs")
def update_prefs(
    body: PrefsBody,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _sync(request, db)
    prefs = normalize_prefs(user.notification_prefs)
    for key in DEFAULT_PREFS:
        value = getattr(body, key)
        if isinstance(value, bool):
            prefs[key] = value
    user.notification_prefs = prefs
    return {"notification_prefs": prefs}


@router.post("/api/me/oath")
def accept_oath(request: Request, user: User = Depends(current_user), db: Session = Depends(db_session)) -> dict:
    _sync(request, db)
    if user.oath_accepted_at is None:
        user.oath_accepted_at = now_utc()
    settle_progression(db, user, now_utc())
    return {"oath_accepted": True, "unlocked": unlocked_codes(db, user)}


@router.post("/api/missions/{cci_setup_id}/mark")
def post_mark(
    cci_setup_id: str,
    body: MarkBody,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _sync(request, db)
    limit(f"mark:{user.id}", 30, 3600)
    try:
        return record_mark(db, user, cci_setup_id, body.kind, now_utc())
    except ActionError as exc:
        raise_action(exc)
