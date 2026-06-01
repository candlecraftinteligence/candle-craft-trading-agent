from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.telegram_admin.client import TelegramAdminConfig  # noqa: E402
from app.telegram_admin.command_processor import (  # noqa: E402
    DEFAULT_ADMIN_COMMAND_AUDIT_PATH,
    DEFAULT_ADMIN_COMMAND_STATE_PATH,
    DEFAULT_COMMAND_LIMIT,
    TelegramAdminCommandTransport,
    process_telegram_admin_commands,
)
from app.telegram_admin.commands import DEFAULT_SCAN_RUN_MANIFEST_PATH, TelegramAdminCommandService  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely process Telegram admin bot commands once.")
    parser.add_argument("--once", action="store_true", help="Process one getUpdates pass and exit.")
    parser.add_argument("--limit", type=int, default=DEFAULT_COMMAND_LIMIT, help="Maximum updates to handle.")
    parser.add_argument("--dry-run", action="store_true", help="Do not read Telegram or send live replies.")
    parser.add_argument("--show-preview", action="store_true", help="Print compact safe response previews.")
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_SCAN_RUN_MANIFEST_PATH)
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
    )
    result = asyncio.run(
        process_telegram_admin_commands(
            config=config,
            command_service=command_service,
            transport=transport,
            state_path=_resolve_project_path(args.state_path),
            audit_path=_resolve_project_path(args.audit_path),
            limit=args.limit,
            dry_run=args.dry_run,
            show_preview=args.show_preview,
        )
    )

    print(f"processor_status={result.delivery_status}")
    print(f"updates_seen={result.updates_seen}")
    print(f"processed_count={result.processed_count}")
    print(f"sent_count={result.sent_count}")
    print(f"audit_path={result.audit_path}")
    print(f"state_path={result.state_path}")
    for index, preview in enumerate(result.previews, start=1):
        print(f"preview_{index}={preview}")
    if result.error_message != "N/A":
        print(f"error_message={result.error_message}")

    real_processing_failure = (
        result.failed
        and config.admin_enabled
        and not config.dry_run
        and not args.dry_run
        and config.has_admin_credentials
    )
    return 1 if real_processing_failure else 0


def _resolve_project_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
