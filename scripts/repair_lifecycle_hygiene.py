from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.data.dtos import NA
from app.lifecycle.hygiene import (
    QUARANTINE_REASON_CODE,
    GeometryHygieneManifest,
    GeometryHygienePlan,
    LifecycleHygieneError,
    apply_invalid_lifecycle_geometry_quarantine,
    audit_invalid_lifecycle_geometry,
    validate_geometry_hygiene_manifest,
)
from app.storage.database import connect_database, open_read_only_database
from app.storage.database import read_only_connection_safety_proof
from app.storage.maintenance import create_verified_backup

WARNING = (
    "WARNING: Use this script only on an explicitly selected, reviewed database copy. "
    "Dry-run is the default and no rows are ever deleted."
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.database_path)
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")
    if args.limit is not None:
        raise SystemExit("--limit is not supported for geometry quarantine; audit the complete database.")

    print(WARNING, file=sys.stderr)
    with open_read_only_database(db_path) as connection:
        read_only_safety_proof = read_only_connection_safety_proof(connection)
        plan = audit_invalid_lifecycle_geometry(connection)

    manifest: GeometryHygieneManifest | None = None
    if args.apply:
        if args.confirm != QUARANTINE_REASON_CODE:
            raise SystemExit("--apply requires --confirm " + QUARANTINE_REASON_CODE)
        if args.manifest is None:
            raise SystemExit("--apply requires --manifest with the separately reviewed audit manifest.")
        manifest = _load_manifest(args.manifest)
        validate_geometry_hygiene_manifest(plan, manifest)
        if not args.no_backup:
            if args.archive_directory is None:
                raise SystemExit(
                    "--archive-directory is required for the automatic verified backup on --apply."
                )
            backup = _create_backup(
                db_path,
                Path(args.archive_directory),
                allow_unsafe_temp=args.allow_unsafe_temp,
            )
            print(f"Verified backup created: {backup['snapshot_path']}", file=sys.stderr)
            print(f"Backup manifest created: {backup['manifest_path']}", file=sys.stderr)

    result = plan
    if args.apply:
        assert manifest is not None
        connection = connect_database(db_path)
        try:
            result = apply_invalid_lifecycle_geometry_quarantine(
                connection,
                plan,
                manifest=manifest,
            )
        finally:
            connection.close()

    _print_geometry_report(result, applied=args.apply)
    result_payload = result.as_dict()
    if not args.apply:
        result_payload["dry_run_safety_proof"] = {
            **read_only_safety_proof,
            "apply_path_entered": False,
        }
    payload = json.dumps(result_payload, sort_keys=True, indent=2)
    if args.json_output is not None:
        Path(args.json_output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


def repair_database(
    connection: sqlite3.Connection,
    *,
    apply: bool = False,
    manifest: GeometryHygieneManifest | Mapping[str, Any] | None = None,
    limit: int | None = None,
    verbose: bool = False,
) -> GeometryHygienePlan:
    """Compatibility API for the focused geometry audit/quarantine workflow."""

    del verbose
    if limit is not None:
        raise LifecycleHygieneError("Lifecycle geometry quarantine always audits the complete database.")
    plan = audit_invalid_lifecycle_geometry(connection)
    if not apply:
        return plan
    if manifest is None:
        raise LifecycleHygieneError(
            "Lifecycle geometry quarantine apply requires an explicit reviewed manifest."
        )
    return apply_invalid_lifecycle_geometry_quarantine(
        connection,
        plan,
        manifest=manifest,
    )


def _load_manifest(path: Path) -> GeometryHygieneManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleHygieneError(f"Unable to read lifecycle hygiene manifest: {path}") from exc
    if not isinstance(payload, Mapping):
        raise LifecycleHygieneError("Lifecycle hygiene manifest must be a JSON object.")
    return GeometryHygieneManifest.from_dict(payload)


def _create_backup(
    db_path: Path,
    archive_directory: Path,
    *,
    allow_unsafe_temp: bool = False,
) -> dict[str, Any]:
    return create_verified_backup(
        db_path,
        archive_directory,
        label="pre-lifecycle-hygiene",
        allow_unsafe_temp=allow_unsafe_temp,
    )


def _print_geometry_report(plan: GeometryHygienePlan, *, applied: bool) -> None:
    action = "applied" if applied else "planned"
    print(
        "Invalid lifecycle geometry audit: "
        f"{len(plan.items)} malformed; "
        f"{len(plan.safe_to_quarantine)} safe to quarantine; "
        f"{len(plan.requires_manual_review)} require manual review; "
        f"{len(plan.historical_preserve)} historical rows preserved; "
        f"{len(plan.safe_to_quarantine) if applied else 0} transitions {action}.",
        file=sys.stderr,
    )
    for item in plan.items:
        transition = item.proposed_state if item.proposed_state != NA else "preserve"
        reasons = ", ".join(item.reasons) if item.reasons else "none"
        ownership = ", ".join(item.dependency_ownership) if item.dependency_ownership else "none"
        print(
            f"- {item.lifecycle_id} {item.symbol} {item.current_state}: "
            f"{item.classification}; {item.geometry_failure}; transition={transition}; "
            f"ownership={ownership}; reasons={reasons}",
            file=sys.stderr,
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit or manifest-gate quarantine of malformed stored lifecycle plan geometry."
    )
    parser.add_argument("--database-path", required=True, help="Explicit SQLite database path to inspect.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply an exact reviewed manifest. Default is read-only dry-run.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help=(
            "Separately reviewed JSON manifest containing lifecycle ID, current plan identity, "
            "expected state, and approved legal transition for every apply target."
        ),
    )
    parser.add_argument(
        "--confirm",
        help="Required confirmation token for --apply: " + QUARANTINE_REASON_CODE,
    )
    parser.add_argument("--json-output", type=Path, help="Optional path for the machine-readable JSON report.")
    parser.add_argument("--backup", action="store_true", help="Accepted for compatibility; backups are automatic on --apply.")
    parser.add_argument("--no-backup", action="store_true", help="Explicitly skip the automatic verified backup on --apply.")
    parser.add_argument(
        "--archive-directory",
        type=Path,
        help="Explicit durable directory for the verified pre-repair snapshot.",
    )
    parser.add_argument(
        "--allow-unsafe-temp",
        action="store_true",
        help="Allow a temporary archive directory for controlled testing only.",
    )
    parser.add_argument("--limit", type=int, help="Limit rows scanned for inspection/testing.")
    parser.add_argument("--verbose", action="store_true", help="Print sample row identifiers.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
