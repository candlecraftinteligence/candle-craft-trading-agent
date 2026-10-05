import hashlib
import hmac
import json
import time
import urllib.parse


class AuthError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int,
    now: int | None = None,
) -> dict:
    """Validate Telegram Mini App initData.

    secret_key = HMAC_SHA256(key="WebAppData", message=bot_token)
    hash = hex HMAC_SHA256(key=secret_key, message=data_check_string)
    """
    if not init_data or not str(init_data).strip():
        raise AuthError("missing")
    if not bot_token:
        raise AuthError("unconfigured")

    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    given = pairs.pop("hash", None)
    if not given:
        raise AuthError("bad_hash")

    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, given):
        raise AuthError("bad_hash")

    try:
        auth_date = int(pairs["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthError("auth_date") from exc

    current = int(time.time()) if now is None else int(now)
    if auth_date > current + 60:
        raise AuthError("auth_date")
    if current - auth_date > max_age_seconds:
        raise AuthError("expired")

    try:
        user = json.loads(pairs["user"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AuthError("user") from exc
    if not isinstance(user, dict) or isinstance(user.get("id"), bool) or not isinstance(user.get("id"), int):
        raise AuthError("user")
    return user
