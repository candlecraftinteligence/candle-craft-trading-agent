import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.security.telegram_auth import AuthError


def issue_session(user_id: uuid.UUID, telegram_user_id: int, secret: str, *, now: datetime | None = None) -> str:
    if not secret:
        raise AuthError("unconfigured")
    current = now or datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "tg": int(telegram_user_id),
        "iat": int(current.timestamp()),
        "exp": int((current + timedelta(hours=12)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def read_session(token: str, secret: str) -> tuple[uuid.UUID, int]:
    if not token or not secret:
        raise AuthError("missing")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        return uuid.UUID(str(payload["sub"])), int(payload["tg"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise AuthError("session") from exc
