"""Evidence-path checks. A rejected path never falls back to a live database."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from app.research.durable_source_replay.constants import LIVE_RUNTIME_DB_NAME, STORE_FORMAT, STORE_SCHEMA_VERSION
from app.storage.database import DEFAULT_DATABASE_PATH


class EvidencePathError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def resolve_evidence_path(
    path: Path | str,
    *,
    operational_paths: Sequence[Path | str] = (),
) -> Path:
    text = str(path).strip()
    if not text:
        raise EvidencePathError("evidence_path_required")
    resolved = Path(text).expanduser().resolve(strict=False)
    name = resolved.name.lower()
    if name.endswith("-wal") or name.endswith("-shm"):
        raise EvidencePathError("wal_or_shm_alias_rejected")
    if name == LIVE_RUNTIME_DB_NAME:
        raise EvidencePathError("live_runtime_database_rejected")
    blocked = [Path(DEFAULT_DATABASE_PATH), *[Path(item) for item in operational_paths]]
    for candidate in blocked:
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(candidate) + suffix) if suffix else candidate
            if _same_path(resolved, target):
                raise EvidencePathError("operational_database_alias_rejected")
    return resolved


def _same_path(left: Path, right: Path) -> bool:
    try:
        left_resolved = left.expanduser().resolve(strict=False)
        right_resolved = right.expanduser().resolve(strict=False)
    except OSError:
        return False
    if left_resolved.exists() and right_resolved.exists():
        try:
            if left_resolved.samefile(right_resolved):
                return True
        except OSError:
            pass
    return os.path.normcase(str(left_resolved)) == os.path.normcase(str(right_resolved))


def inspect_existing_store_file(path: Path) -> str:
    """Return ``new``, ``readable``, or raise when the file must not be used."""

    if not path.exists():
        return "new"
    if not path.is_file():
        raise EvidencePathError("evidence_path_not_a_file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise EvidencePathError(f"evidence_path_unreadable:{type(exc).__name__}") from exc
    if size == 0:
        raise EvidencePathError("empty_file_rejected")
    try:
        header = path.read_bytes()[:16]
    except OSError as exc:
        raise EvidencePathError(f"evidence_path_unreadable:{type(exc).__name__}") from exc
    if not header.startswith(b"SQLite format 3\x00"):
        raise EvidencePathError("unrelated_file_rejected")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            raise EvidencePathError("unrelated_sqlite_database_rejected")
        if version != STORE_SCHEMA_VERSION:
            raise EvidencePathError(f"unsupported_store_version:{version}")
        row = connection.execute(
            "SELECT meta_value FROM evidence_meta WHERE meta_key = 'format'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise EvidencePathError("unrelated_sqlite_database_rejected") from exc
    finally:
        connection.close()
    if row is None or str(row[0]) != STORE_FORMAT:
        raise EvidencePathError("unrelated_sqlite_database_rejected")
    return "readable"


def footprint_bytes(path: Path) -> int:
    total = 0
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            if candidate.exists():
                total += candidate.stat().st_size
        except OSError:
            continue
    return total
