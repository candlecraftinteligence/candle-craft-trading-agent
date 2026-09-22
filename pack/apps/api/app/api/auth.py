from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_host, db_session, limit, prepare, settings_dep
from app.db.models import User
from app.security.sessions import issue_session
from app.security.telegram_auth import AuthError, validate_init_data
from app.settings import Settings

router = APIRouter()

DEV_TELEGRAM_ID = 900000001


class InitBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    init_data: str = Field(default="", max_length=8192)


def _touch_user(db: Session, telegram_id: int, username: str | None, display_name: str, now: datetime) -> User:
    user = db.scalar(select(User).where(User.telegram_user_id == telegram_id))
    if user is None:
        user = User(
            telegram_user_id=telegram_id,
            username=username,
            display_name=display_name,
            created_at=now,
            last_seen_at=now,
        )
        db.add(user)
        db.flush()
        return user
    if username:
        user.username = username
    if display_name:
        user.display_name = display_name
    user.last_seen_at = now
    return user


def _token(user: User, settings: Settings) -> dict:
    try:
        token = issue_session(user.id, user.telegram_user_id, settings.session_secret)
    except AuthError as exc:
        raise HTTPException(status_code=503, detail="Session secret is not configured.") from exc
    return {
        "token": token,
        "user": {
            "display_name": user.display_name,
            "telegram_user_id": user.telegram_user_id,
            "pack_xp": int(user.pack_xp),
            "wolf_rank": user.wolf_rank,
        },
    }


@router.post("/api/auth/telegram")
def auth_telegram(
    body: InitBody,
    request: Request,
    db: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict:
    limit(f"auth:{client_host(request)}", 30, 60)
    try:
        tg_user = validate_init_data(
            body.init_data,
            settings.bot_token,
            max_age_seconds=settings.auth_max_age_seconds,
        )
    except AuthError as exc:
        raise HTTPException(status_code=401, detail="Telegram initData was rejected.") from exc
    prepare(request, db)
    name = str(tg_user.get("first_name") or "Pack member")
    last = tg_user.get("last_name")
    if last:
        name = f"{name} {last}"
    username = tg_user.get("username")
    user = _touch_user(
        db,
        int(tg_user["id"]),
        str(username) if username else None,
        name[:128],
        datetime.now(timezone.utc),
    )
    return _token(user, settings)


@router.post("/api/auth/dev")
def auth_dev(
    request: Request,
    db: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> dict:
    limit(f"auth:{client_host(request)}", 30, 60)
    if not settings.dev_browser_mode:
        raise HTTPException(status_code=404, detail="Not found.")
    prepare(request, db)
    user = _touch_user(db, DEV_TELEGRAM_ID, "dev", "Dev Operator", datetime.now(timezone.utc))
    return _token(user, settings)
