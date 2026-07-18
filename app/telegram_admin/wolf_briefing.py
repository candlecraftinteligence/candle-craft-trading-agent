from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.data.dtos import NA
from app.storage.database import StorageError, open_read_only_database


@dataclass(frozen=True)
class WolfScanArtifacts:
    manifest_row: Mapping[str, Any] | None
    scan_payload: Mapping[str, Any] | None
    source_path: Path | None = None


def load_latest_db_scan_artifacts(
    *,
    project_root: Path | str,
    database_path: Path | str,
) -> WolfScanArtifacts:
    """Load the latest persisted scan row from SQLite without mutating it."""

    root = Path(project_root)
    path = _resolve_project_path(root, database_path)
    if not path.exists() or not path.is_file():
        return WolfScanArtifacts(manifest_row=None, scan_payload=None, source_path=path)

    try:
        with _connect_readonly(path) as connection:
            if not _table_exists(connection, "scan_runs"):
                return WolfScanArtifacts(manifest_row=None, scan_payload=None, source_path=path)
            columns = _table_columns(connection, "scan_runs")
            required = {
                "run_id",
                "timestamp",
                "symbols_scanned",
                "market_regime",
                "regime_confidence",
                "total_valid_setups",
                "near_misses",
                "rejected",
                "raw_payload_json",
            }
            if not required <= columns:
                return WolfScanArtifacts(manifest_row=None, scan_payload=None, source_path=path)
            row = connection.execute(
                """
                SELECT run_id, timestamp, symbols_scanned, market_regime, regime_confidence,
                       total_valid_setups, near_misses, rejected, raw_payload_json
                FROM scan_runs
                ORDER BY timestamp DESC
                LIMIT 1
                """
            ).fetchone()
    except (OSError, StorageError, sqlite3.Error):
        return WolfScanArtifacts(manifest_row=None, scan_payload=None, source_path=path)

    if row is None:
        return WolfScanArtifacts(manifest_row=None, scan_payload=None, source_path=path)

    payload = _json_mapping(row["raw_payload_json"])
    manifest = {
        "run_id": _display(row["run_id"]),
        "timestamp": _display(row["timestamp"]),
        "symbols_scanned": _display(row["symbols_scanned"]),
        "market_regime": _display(row["market_regime"]),
        "regime_confidence": _display(row["regime_confidence"]),
        "valid_setup_count": _display(row["total_valid_setups"]),
        "near_miss_count": _display(row["near_misses"]),
        "rejected_count": _display(row["rejected"]),
    }
    return WolfScanArtifacts(manifest_row=manifest, scan_payload=payload, source_path=path)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return open_read_only_database(path)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _resolve_project_path(project_root: Path, value: Path | str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def _json_mapping(value: Any) -> Mapping[str, Any] | None:
    text = _display(value)
    if text == NA:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    return str(value)


__all__ = [
    "WolfScanArtifacts",
    "load_latest_db_scan_artifacts",
]
