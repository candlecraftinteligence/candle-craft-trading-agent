import os
from dataclasses import dataclass


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    bot_token: str
    telegram_bot_token: str
    session_secret: str
    auth_max_age_seconds: int
    dev_browser_mode: bool
    pack_env: str
    live_cci: bool
    bot_required: bool
    mini_app_url: str


def load_settings() -> Settings:
    pack_env = os.environ.get("PACK_ENV", os.environ.get("APP_ENV", "dev")).strip().lower() or "dev"
    app_env = os.environ.get("APP_ENV", pack_env).strip().lower() or pack_env
    production = pack_env == "production" or app_env == "production"
    dev_requested = _flag("DEV_BROWSER_MODE")
    try:
        max_age = int(os.environ.get("AUTH_MAX_AGE_SECONDS", "86400"))
    except ValueError:
        max_age = 86400
    telegram_bot_token = os.environ.get("PACK_TELEGRAM_BOT_TOKEN", "").strip()
    legacy_bot_token = os.environ.get("PACK_BOT_TOKEN", "").strip()
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://pack:pack@127.0.0.1:5432/pack",
        ),
        bot_token=telegram_bot_token or legacy_bot_token,
        telegram_bot_token=telegram_bot_token,
        session_secret=os.environ.get("PACK_SESSION_SECRET", "").strip(),
        auth_max_age_seconds=max_age,
        dev_browser_mode=dev_requested and not production,
        pack_env=pack_env,
        live_cci=_flag("LIVE_CCI"),
        bot_required=_flag("BOT_REQUIRED"),
        mini_app_url=os.environ.get("PACK_MINI_APP_URL", "").strip(),
    )
