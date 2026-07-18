from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.storage.database import StorageError, WRITABLE_BUSY_TIMEOUT_MS
from app.storage.maintenance import (
    MaintenanceError,
    WarningThresholds,
    check_database,
    checkpoint_database,
    create_verified_backup,
    inspect_database,
    plan_backup,
    verify_backup,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "inspect":
            report = inspect_database(
                args.database_path,
                thresholds=_thresholds(args),
                archive_directory=args.archive_directory,
            )
            _print_json(report)
            return 1 if report["schema_status"] in {"unsupported-newer", "unversioned"} else 0
        if args.command == "quick-check":
            report = check_database(args.database_path, full=False)
            _print_json(report)
            return 0 if report["ok"] else 1
        if args.command == "full-check":
            report = check_database(args.database_path, full=True)
            _print_json(report)
            return 0 if report["ok"] else 1
        if args.command == "backup":
            return _backup(args)
        if args.command == "backup-verify":
            report = verify_backup(args.snapshot_path, manifest_path=args.manifest_path)
            _print_json(report)
            return 0 if report["ok"] else 1
        if args.command == "checkpoint":
            report = checkpoint_database(
                args.database_path,
                mode=args.mode,
                busy_timeout_ms=args.busy_timeout_ms,
            )
            _print_json(report)
            return 0 if report["complete"] else 2
    except (MaintenanceError, StorageError, OSError, ValueError) as exc:
        print(f"SQLite maintenance failed: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"Unhandled SQLite maintenance command: {args.command}")


def _backup(args: argparse.Namespace) -> int:
    created = datetime.now(UTC)
    suffix = uuid4().hex[:12]
    plan = plan_backup(
        args.database_path,
        args.archive_directory,
        label=args.label,
        allow_unsafe_temp=args.allow_unsafe_temp,
        now=created,
        unique_suffix=suffix,
    )
    print(f"Source database: {plan['source_database']}")
    print(f"Archive directory: {plan['archive_directory']}")
    print(f"Snapshot destination: {plan['snapshot_path']}")
    print(f"Manifest destination: {plan['manifest_path']}")
    if args.dry_run:
        print("Dry run: validation completed; no files will be written.")

    def progress(copied: int, total: int) -> None:
        print(f"Backup progress: {copied}/{total} pages", file=sys.stderr)

    report = create_verified_backup(
        args.database_path,
        args.archive_directory,
        label=args.label,
        dry_run=args.dry_run,
        allow_unsafe_temp=args.allow_unsafe_temp,
        progress=progress,
        now=created,
        unique_suffix=suffix,
    )
    _print_json(report)
    return 0


def _thresholds(args: argparse.Namespace) -> WarningThresholds:
    return WarningThresholds(
        low_free_bytes=max(0, args.low_free_bytes),
        low_free_percent=max(0.0, args.low_free_percent),
        large_wal_bytes=max(0, args.large_wal_bytes),
        rapid_growth_bytes_per_day=max(0, args.rapid_growth_bytes_per_day),
        integrity_max_age_hours=max(0.0, args.integrity_max_age_hours),
        backup_max_age_hours=max(0.0, args.backup_max_age_hours),
    )


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only SQLite diagnostics and verified online backup maintenance."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Read database, sidecar, table, growth, and disk diagnostics without writes."
    )
    _database_argument(inspect_parser)
    inspect_parser.add_argument(
        "--archive-directory",
        type=Path,
        help="Optional archive directory whose verified manifests provide backup/growth history.",
    )
    defaults = WarningThresholds()
    inspect_parser.add_argument("--low-free-bytes", type=int, default=defaults.low_free_bytes)
    inspect_parser.add_argument("--low-free-percent", type=float, default=defaults.low_free_percent)
    inspect_parser.add_argument("--large-wal-bytes", type=int, default=defaults.large_wal_bytes)
    inspect_parser.add_argument(
        "--rapid-growth-bytes-per-day",
        type=int,
        default=defaults.rapid_growth_bytes_per_day,
    )
    inspect_parser.add_argument(
        "--integrity-max-age-hours",
        type=float,
        default=defaults.integrity_max_age_hours,
    )
    inspect_parser.add_argument(
        "--backup-max-age-hours",
        type=float,
        default=defaults.backup_max_age_hours,
    )

    quick_parser = subparsers.add_parser(
        "quick-check", help="Run read-only quick_check and foreign_key_check."
    )
    _database_argument(quick_parser)

    full_parser = subparsers.add_parser(
        "full-check", help="Explicitly run the potentially long read-only integrity_check."
    )
    _database_argument(full_parser)

    backup_parser = subparsers.add_parser(
        "backup", help="Create and verify a SQLite online snapshot with a SHA-256 manifest."
    )
    _database_argument(backup_parser)
    backup_parser.add_argument("--archive-directory", type=Path, required=True)
    backup_parser.add_argument("--label", help="Optional short human-readable filename label.")
    backup_parser.add_argument("--dry-run", action="store_true")
    backup_parser.add_argument(
        "--allow-unsafe-temp",
        action="store_true",
        help="Explicitly allow a temporary archive directory (intended for controlled testing only).",
    )

    verify_parser = subparsers.add_parser(
        "backup-verify", help="Verify an existing snapshot against its JSON manifest."
    )
    verify_parser.add_argument("--snapshot-path", type=Path, required=True)
    verify_parser.add_argument("--manifest-path", type=Path)

    checkpoint_parser = subparsers.add_parser(
        "checkpoint", help="Explicitly request a bounded writable WAL checkpoint."
    )
    _database_argument(checkpoint_parser)
    checkpoint_parser.add_argument(
        "--mode",
        choices=("PASSIVE", "FULL", "RESTART", "TRUNCATE"),
        default="PASSIVE",
    )
    checkpoint_parser.add_argument(
        "--busy-timeout-ms",
        type=int,
        default=WRITABLE_BUSY_TIMEOUT_MS,
    )
    return parser.parse_args(argv)


def _database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-path", type=Path, required=True)


if __name__ == "__main__":
    raise SystemExit(main())
