from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.telegram_admin import (  # noqa: E402
    TelegramAdminClient,
    TelegramAdminConfig,
    TelegramAdminDelivery,
    TelegramAdminTransport,
)


DEFAULT_TEST_MESSAGE = "Candle Craft admin delivery test"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely test admin-only Telegram delivery.")
    parser.add_argument("--message", default=DEFAULT_TEST_MESSAGE)
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode for this test.")
    parser.add_argument(
        "--force-live",
        action="store_true",
        help="Send live only when TELEGRAM_ADMIN_REPORTS_ENABLED=true and TELEGRAM_DRY_RUN=false.",
    )
    parser.add_argument("--show-preview", action="store_true")
    return parser.parse_args(argv)


async def run_delivery_test(
    *,
    message: str,
    settings: Settings | None = None,
    dry_run: bool = False,
    force_live: bool = False,
    transport: TelegramAdminTransport | None = None,
) -> tuple[TelegramAdminConfig, TelegramAdminDelivery]:
    base_config = TelegramAdminConfig.from_settings(settings or Settings())
    config = _effective_config(base_config, dry_run=dry_run, force_live=force_live)
    client = TelegramAdminClient(config, transport=transport)
    delivery = await client.send_admin_report(message)
    return config, delivery


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    transport: TelegramAdminTransport | None = None,
) -> int:
    args = parse_args(argv)
    message = args.message.strip()
    if not message:
        message = DEFAULT_TEST_MESSAGE

    config, delivery = asyncio.run(
        run_delivery_test(
            message=message,
            settings=settings,
            dry_run=args.dry_run,
            force_live=args.force_live,
            transport=transport,
        )
    )

    if args.show_preview:
        print(f"message_preview={_preview(message)}")

    print(f"admin_reports_enabled={_bool_text(config.admin_report_enabled)}")
    print(f"legacy_admin_enabled={_bool_text(config.admin_enabled)}")
    print(f"dry_run={_bool_text(config.dry_run)}")
    print(f"admin_chat_id_present={_bool_text(bool(config.admin_chat_id))}")
    print(f"bot_token_present={_bool_text(bool(config.bot_token))}")
    print(f"delivery_status={delivery.status}")

    real_delivery_attempted = config.admin_report_enabled and not config.dry_run and config.has_admin_credentials
    if delivery.status == "failed" and real_delivery_attempted:
        return 1
    return 0


def _effective_config(
    config: TelegramAdminConfig,
    *,
    dry_run: bool,
    force_live: bool,
) -> TelegramAdminConfig:
    live_allowed_by_config = config.admin_report_enabled and not config.dry_run
    effective_dry_run = True
    if force_live and live_allowed_by_config:
        effective_dry_run = False
    if dry_run:
        effective_dry_run = True
    return replace(config, dry_run=effective_dry_run)


def _preview(message: str, max_length: int = 240) -> str:
    compact = " ".join(message.split())
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 3].rstrip()}..."


def _bool_text(value: bool) -> str:
    return str(value).lower()


if __name__ == "__main__":
    raise SystemExit(main())
