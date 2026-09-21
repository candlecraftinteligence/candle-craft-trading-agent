import uuid
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timezone
from time import monotonic

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db.models import User
from app.db.session import session_factory
from app.security.sessions import read_session
from app.security.telegram_auth import AuthError
from app.services.actions import ActionError
from app.services.sync import seed_reference, sync_missions
from app.settings import Settings, load_settings

_bearer = HTTPBearer(auto_error=False)
_hits: dict[str, list[float]] = defaultdict(list)


def settings_dep() -> Settings:
    return load_settings()


def db_session() -> Iterator[Session]:
    db = session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def limit(key: str, max_hits: int, window_seconds: float) -> None:
    now = monotonic()
    recent = [stamp for stamp in _hits[key] if now - stamp < window_seconds]
    if len(recent) >= max_hits:
        raise HTTPException(status_code=429, detail="Slow down. The den is still here.")
    recent.append(now)
    _hits[key] = recent


def prepare(request: Request, db: Session) -> None:
    try:
        seed_reference(db)
        sync_missions(db, request.app.state.mission_source)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Pack database is unavailable.") from exc


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing initData session.")
    try:
        user_id, _telegram_id = read_session(credentials.credentials, settings.session_secret)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail="Missing initData session.") from exc
    user = db.get(User, user_id)
    if user is None or not isinstance(user.id, uuid.UUID):
        raise HTTPException(status_code=401, detail="Missing initData session.")
    request.state.user = user
    return user


def raise_action(exc: ActionError) -> None:
    raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
