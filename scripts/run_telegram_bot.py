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

from app.alerts.telegram import DEFAULT_TELEGRAM_TIMEOUT, TELEGRAM_API_BASE_URL  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.data.dtos import NA  # noqa: E402
from app.storage.database import DEFAULT_DATABASE_PATH  # noqa: E402
from app.telegram_admin.client import TelegramAdminConfig  # noqa: E402
from app.telegram_admin.command_processor import (  # noqa: E402
    DEFAULT_ADMIN_COMMAND_AUDIT_PATH,
    DEFAULT_ADMIN_COMMAND_STATE_PATH,
    DEFAULT_COMMAND_LIMIT,
    DEFAULT_GET_UPDATES_TIMEOUT_SECONDS,
    TelegramAdminCommandTransport,
    process_telegram_admin_commands,
)
from app.telegram_admin.commands import DEFAULT_SCAN_RUN_MANIFEST_PATH, TelegramAdminCommandService  # noqa: E402


COMMANDS_TO_REGISTER: tuple[Mapping[str, str], ...] = (
    {"command": "start", "description": "Open the Candle Craft menu"},
    {"command": "menu", "description": "Open the Candle Craft menu"},
    {"command": "status", "description": "Show bot status"},
    {"command": "latest", "description": "Show latest public lifecycle alerts"},
    {"command": "about", "description": "About Candle Craft"},
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Candle Craft Telegram UI listener only.")
    parser.add_argument("--once", action="store_true", help="Process one polling pass and exit.")
    parser.add_argument("--max-iterations", type=int, help="Stop after this many polling passes.")
    parser.add_argument("--limit", type=int, default=DEFAULT_COMMAND_LIMIT, help="Maximum updates per pass.")
    parser.add_argument(
        "--get-updates-timeout",
        type=int,
        default=max(10, DEFAULT_GET_UPDATES_TIMEOUT_SECONDS),
        help="Telegram long-poll timeout in seconds.",
    )
    parser.add_argument("--poll-interval-sec", type=float, default=2.0, help="Delay between polling passes.")
    parser.add_argument("--dry-run", action="store_true", help="Do not read Telegram or send live replies.")
    parser.add_argument("--show-preview", action="store_true", help="Print compact safe response previews.")
    parser.add_argument("--register-commands", action="store_true", help="Register the public bot slash commands.")
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_SCAN_RUN_MANIFEST_PATH)
    parser.add_argument("--database-path", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_ADMIN_COMMAND_STATE_PATH)
    parser.add_argument("--audit-path", type=Path, default=DEFAULT_ADMIN_COMMAND_AUDIT_PATH)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    transport: TelegramAdminCommandTransport | None = None,
) -> int:
    args = parse_args(argv)
    config = TelegramAdminConfig.from_settings(settings or Settings())
    command_service = TelegramAdminCommandService(
        project_root=PROJECT_ROOT,
        manifest_path=args.manifest_path,
        database_path=args.database_path,
    )

    print("Telegram UI listener process.")
    print("Scanner is not started by this process.")
    print(f"commands_enabled={_bool_text(config.command_ui_enabled)}")
    print(f"public_ui_enabled={_bool_text(config.public_command_ui_enabled)}")
    print(f"dry_run={_bool_text(config.dry_run or args.dry_run)}")
    print(f"admin_chat_configured={_bool_text(bool(config.admin_chat_id))}")
    print(f"public_chat_configured={_bool_text(bool(config.public_chat_id))}")
    print(f"public_channel_configured={_bool_text(bool(config.public_channel_id))}")

    if not config.command_ui_enabled:
        print("Telegram commands disabled. Set TELEGRAM_COMMANDS_ENABLED=true and rerun this listener.")
        return 0

    try:
        if args.register_commands:
            registration = asyncio.run(register_telegram_commands(config=config, dry_run=args.dry_run))
            print(f"register_commands_status={registration['status']}")
            if registration.get("error_message") not in {None, NA}:
                print(f"register_commands_error={registration['error_message']}")

        asyncio.run(
            run_polling_loop(
                config=config,
                command_service=command_service,
                transport=transport,
                state_path=_resolve_project_path(args.state_path),
                audit_path=_resolve_project_path(args.audit_path),
                limit=args.limit,
                get_updates_timeout=args.get_updates_timeout,
                poll_interval_sec=args.poll_interval_sec,
                dry_run=args.dry_run,
                show_preview=args.show_preview,
                once=args.once,
                max_iterations=args.max_iterations,
            )
        )
    except KeyboardInterrupt:
        print("Telegram UI listener stopped by user.")
        return 0
    return 0


async def run_polling_loop(
    *,
    config: TelegramAdminConfig,
    command_service: TelegramAdminCommandService,
    transport: TelegramAdminCommandTransport | None = None,
    state_path: Path,
    audit_path: Path,
    limit: int,
    get_updates_timeout: int,
    poll_interval_sec: float,
    dry_run: bool,
    show_preview: bool,
    once: bool,
    max_iterations: int | None,
) -> None:
    iteration = 0
    while True:
        iteration += 1
        result = await process_telegram_admin_commands(
            config=config,
            command_service=command_service,
            transport=transport,
            state_path=state_path,
            audit_path=audit_path,
            limit=limit,
            dry_run=dry_run,
            show_preview=show_preview,
            get_updates_timeout=get_updates_timeout,
        )
        print(
            "poll_status="
            f"{result.delivery_status}; updates_seen={result.updates_seen}; "
            f"processed={result.processed_count}; sent={result.sent_count}"
        )
        for index, preview in enumerate(result.previews, start=1):
            print(f"preview_{index}={preview}")
        if result.error_message != NA:
            print(f"poll_error={result.error_message}")

        if once or (max_iterations is not None and iteration >= max_iterations):
            break
        await asyncio.sleep(max(0.1, poll_interval_sec))


async def register_telegram_commands(
    *,
    config: TelegramAdminConfig,
    dry_run: bool = False,
    api_base_url: str = TELEGRAM_API_BASE_URL,
    timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
) -> Mapping[str, str]:
    if dry_run or config.dry_run:
        return {"status": "dry_run"}
    if not config.bot_token:
        return {"status": "skipped_missing_credentials"}

    async with httpx.AsyncClient(base_url=api_base_url, timeout=timeout) as client:
        try:
            response = await client.post(
                f"/bot{config.bot_token}/setMyCommands",
                json={"commands": list(COMMANDS_TO_REGISTER)},
            )
        except httpx.TimeoutException:
            return {"status": "failed", "error_message": "Telegram command registration timed out."}
        except httpx.HTTPError as exc:
            return {"status": "failed", "error_message": _sanitize_error(exc, config)}

    if response.status_code != 200:
        return {"status": "failed", "error_message": f"Telegram returned HTTP {response.status_code}."}
    try:
        payload = response.json()
    except ValueError:
        return {"status": "failed", "error_message": "Telegram command registration response could not be read."}
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        description = payload.get("description") if isinstance(payload, Mapping) else None
        return {"status": "failed", "error_message": _sanitize_error(description or "Telegram did not confirm registration.", config)}
    return {"status": "registered"}


def _resolve_project_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _sanitize_error(value: Any, config: TelegramAdminConfig) -> str:
    text = str(value or "").strip()
    if not text:
        return NA
    for secret in (
        config.bot_token,
        config.admin_chat_id,
        config.public_chat_id,
        config.public_channel_id,
        config.vip_channel_id,
    ):
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    return text


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


if __name__ == "__main__":
    raise SystemExit(main())
