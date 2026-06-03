from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Sequence

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.telegram_admin.client import TelegramAdminConfig  # noqa: E402
from app.telegram_admin.native_menu import clear_telegram_native_command_menu  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clear Telegram native slash commands while keeping Candle Craft reply keyboards."
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate config without calling Telegram.")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> int:
    args = parse_args(argv)
    config = TelegramAdminConfig.from_settings(settings or Settings())
    result = asyncio.run(
        clear_telegram_native_command_menu(
            config=config,
            http_client=http_client,
            dry_run=args.dry_run,
        )
    )

    print(f"native_command_menu_status={result.status}")
    print(f"delete_commands_status={result.delete_commands_status}")
    print(f"menu_button_status={result.menu_button_status}")
    if result.error_message != "N/A":
        print(f"error_message={result.error_message}")

    return 1 if result.failed and not args.dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
