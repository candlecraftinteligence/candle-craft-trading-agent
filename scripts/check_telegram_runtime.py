from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.alerts.telegram import DEFAULT_TELEGRAM_TIMEOUT, TELEGRAM_API_BASE_URL, send_telegram_messages  # noqa: E402
from app.alerts.telegram_sender import resolve_public_signal_destination  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.data.dtos import NA  # noqa: E402
from app.telegram_admin.client import TelegramAdminConfig  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the safe local Telegram runtime configuration.")
    parser.add_argument("--skip-getme", action="store_true", help="Skip the Telegram getMe API check.")
    parser.add_argument("--send-admin-test", action="store_true", help="Send an explicit admin test message.")
    parser.add_argument("--send-public-test", action="store_true", help="Send an explicit public test message.")
    parser.add_argument("--api-base-url", default=TELEGRAM_API_BASE_URL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TELEGRAM_TIMEOUT)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> int:
    args = parse_args(argv)
    try:
        runtime_settings = settings or Settings()
    except Exception as exc:
        print("settings_status=blocked")
        print(f"settings_error={_safe_text(exc)}")
        return 1

    admin_config = TelegramAdminConfig.from_settings(runtime_settings)
    public_destination = resolve_public_signal_destination(runtime_settings)
    listener_script = PROJECT_ROOT / "scripts" / "run_telegram_bot.py"

    print("Telegram runtime diagnostic.")
    print(f"telegram_bot_token={_token_status(runtime_settings.telegram_bot_token)}")
    print(f"admin_chat_configured={_bool_text(bool(admin_config.admin_chat_id))}")
    print(f"public_chat_configured={_bool_text(bool(admin_config.public_chat_id))}")
    print(f"public_channel_configured={_bool_text(bool(admin_config.public_channel_id))}")
    print(f"public_destination_source={public_destination.source}")
    if public_destination.warning != NA and runtime_settings.telegram_signals_enabled:
        print(f"public_destination_warning={public_destination.warning}")
    print(f"signals_enabled={_bool_text(runtime_settings.telegram_signals_enabled)}")
    print(f"commands_enabled={_bool_text(admin_config.command_ui_enabled)}")
    print(f"public_ui_enabled={_bool_text(admin_config.public_command_ui_enabled)}")
    print(f"admin_enabled={_bool_text(admin_config.admin_enabled)}")
    print(f"admin_reports_enabled={_bool_text(admin_config.admin_report_enabled)}")
    print(f"dry_run={_bool_text(admin_config.dry_run)}")
    print(f"local_manual_mode={_bool_text(runtime_settings.local_manual_mode)}")
    print(f"order_execution_enabled={_bool_text(runtime_settings.order_execution_enabled)}")
    print(f"command_listener_script_present={_bool_text(listener_script.exists())}")
    print(
        "command_listener_configured="
        f"{_bool_text(admin_config.command_ui_enabled and listener_script.exists())}"
    )
    if admin_config.command_ui_enabled:
        print("command_listener_note=Run scripts/run_telegram_bot.py to make Telegram buttons respond.")

    exit_code = 0
    if args.skip_getme:
        print("getme_status=skipped")
    elif runtime_settings.telegram_bot_token:
        getme = asyncio.run(
            telegram_get_me(
                bot_token=runtime_settings.telegram_bot_token,
                http_client=http_client,
                api_base_url=args.api_base_url,
                timeout=args.timeout,
                admin_config=admin_config,
            )
        )
        print(f"getme_status={getme['status']}")
        if getme.get("bot_username") not in {None, NA}:
            print(f"getme_bot_username={getme['bot_username']}")
        if getme.get("error_message") not in {None, NA}:
            print(f"getme_error={getme['error_message']}")
            exit_code = 1
    else:
        print("getme_status=skipped_missing_token")

    if args.send_admin_test:
        admin_status = asyncio.run(
            _send_explicit_test_message(
                bot_token=runtime_settings.telegram_bot_token,
                chat_id=admin_config.admin_chat_id,
                dry_run=admin_config.dry_run,
                label="admin",
                http_client=http_client,
                api_base_url=args.api_base_url,
                timeout=args.timeout,
                admin_config=admin_config,
            )
        )
        print(f"admin_test_status={admin_status['status']}")
        if admin_status.get("error_message") not in {None, NA}:
            print(f"admin_test_error={admin_status['error_message']}")
            exit_code = 1

    if args.send_public_test:
        public_status = asyncio.run(
            _send_explicit_test_message(
                bot_token=runtime_settings.telegram_bot_token,
                chat_id=public_destination.chat_id,
                dry_run=admin_config.dry_run,
                label="public",
                http_client=http_client,
                api_base_url=args.api_base_url,
                timeout=args.timeout,
                admin_config=admin_config,
            )
        )
        print(f"public_test_status={public_status['status']}")
        if public_status.get("error_message") not in {None, NA}:
            print(f"public_test_error={public_status['error_message']}")
            exit_code = 1

    return exit_code


async def telegram_get_me(
    *,
    bot_token: str,
    http_client: httpx.AsyncClient | None = None,
    api_base_url: str = TELEGRAM_API_BASE_URL,
    timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
    admin_config: TelegramAdminConfig,
) -> Mapping[str, str]:
    close_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=api_base_url, timeout=timeout)
    try:
        try:
            response = await client.get(f"/bot{bot_token}/getMe")
        except httpx.TimeoutException:
            return {"status": "failed", "error_message": "Telegram getMe request timed out."}
        except httpx.HTTPError as exc:
            return {"status": "failed", "error_message": _sanitize_error(exc, admin_config)}

        if response.status_code != 200:
            return {"status": "failed", "error_message": f"Telegram returned HTTP {response.status_code}."}
        try:
            payload = response.json()
        except ValueError:
            return {"status": "failed", "error_message": "Telegram getMe response could not be read."}
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            description = payload.get("description") if isinstance(payload, Mapping) else None
            return {
                "status": "failed",
                "error_message": _sanitize_error(description or "Telegram did not confirm getMe.", admin_config),
            }
        result = payload.get("result")
        username = result.get("username") if isinstance(result, Mapping) else None
        return {"status": "ok", "bot_username": str(username) if username else NA}
    finally:
        if close_client:
            await client.aclose()


async def _send_explicit_test_message(
    *,
    bot_token: str | None,
    chat_id: str | None,
    dry_run: bool,
    label: str,
    http_client: httpx.AsyncClient | None,
    api_base_url: str,
    timeout: float,
    admin_config: TelegramAdminConfig,
) -> Mapping[str, str]:
    if dry_run:
        return {"status": "skipped_dry_run"}
    if not bot_token or not chat_id:
        return {"status": "skipped_missing_credentials"}
    try:
        results = await send_telegram_messages(
            bot_token=bot_token,
            chat_id=chat_id,
            message=f"Candle Craft {label} Telegram runtime test.",
            http_client=http_client,
            api_base_url=api_base_url,
            timeout=timeout,
        )
    except Exception as exc:
        return {"status": "failed", "error_message": _sanitize_error(exc, admin_config, extra=(chat_id,))}
    error = _first_error(results, admin_config=admin_config, extra=(chat_id,))
    if error != NA:
        return {"status": "failed", "error_message": error}
    return {"status": "sent"}


def _first_error(
    results: Sequence[Mapping[str, Any]],
    *,
    admin_config: TelegramAdminConfig,
    extra: Sequence[str] = (),
) -> str:
    if not results:
        return "telegram_test_send_failed"
    for result in results:
        if result.get("status") != "sent":
            return _sanitize_error(result.get("error") or "telegram_test_send_failed", admin_config, extra=extra)
    return NA


def _token_status(value: str | None) -> str:
    return "present ([REDACTED])" if str(value or "").strip() else "missing"


def _sanitize_error(value: Any, config: TelegramAdminConfig, *, extra: Sequence[str] = ()) -> str:
    text = _safe_text(value)
    for secret in (
        config.bot_token,
        config.admin_chat_id,
        config.public_chat_id,
        config.public_channel_id,
        config.vip_channel_id,
        *extra,
    ):
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    return text


def _safe_text(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else NA


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


if __name__ == "__main__":
    raise SystemExit(main())
