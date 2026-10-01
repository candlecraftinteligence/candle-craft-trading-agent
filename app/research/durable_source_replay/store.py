"""Versioned SQLite evidence store, separate from the operational database.

Writes are one bundle transaction. Readers never migrate. Unknown versions and
unrelated databases are rejected instead of being reused.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.research.durable_source_replay.bounds import BoundExceeded, BoundLimits, DEFAULT_BOUNDS
from app.research.durable_source_replay.codec import content_hash
from app.research.durable_source_replay.constants import (
    CODEC_VERSION,
    STORE_FORMAT,
    STORE_SCHEMA_VERSION,
)
from app.research.durable_source_replay.paths import (
    EvidencePathError,
    footprint_bytes,
    inspect_existing_store_file,
    resolve_evidence_path,
)

_SCHEMA = """
CREATE TABLE evidence_meta (
    meta_key TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);
CREATE TABLE payloads (
    content_hash TEXT PRIMARY KEY,
    codec_version TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    canonical_bytes BLOB NOT NULL
);
CREATE TABLE captures (
    capture_id TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    caller_path TEXT NOT NULL,
    evidence_lineage TEXT NOT NULL,
    codec_version TEXT NOT NULL,
    store_schema_version INTEGER NOT NULL,
    application_schema_version INTEGER NOT NULL,
    build_git_sha TEXT NOT NULL,
    build_dirty TEXT NOT NULL,
    implementation_fingerprint TEXT NOT NULL,
    dependency_versions_json TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_family TEXT NOT NULL,
    policy_supported INTEGER NOT NULL,
    capture_status TEXT NOT NULL,
    incomplete_reason TEXT,
    transaction_status TEXT NOT NULL,
    savepoint_name TEXT,
    savepoint_disposition TEXT NOT NULL,
    enclosing_disposition TEXT NOT NULL,
    exception_class TEXT,
    exception_message TEXT,
    reference_count INTEGER NOT NULL,
    parent_depth INTEGER NOT NULL
);
CREATE TABLE capture_payloads (
    capture_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (capture_id, role, ordinal)
);
CREATE TABLE capture_diagnostics (
    diagnostic_id TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    lifecycle_id TEXT,
    detail TEXT NOT NULL,
    transaction_status TEXT NOT NULL
);
"""


class EvidenceStoreError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def write_bundle(
    path: Path | str,
    *,
    captures: Sequence[Mapping[str, Any]],
    diagnostics: Sequence[Mapping[str, Any]] = (),
    operational_paths: Sequence[Path | str] = (),
    limits: BoundLimits = DEFAULT_BOUNDS,
) -> None:
    resolved = resolve_evidence_path(path, operational_paths=operational_paths)
    state = inspect_existing_store_file(resolved)
    incoming = 0
    for capture in captures:
        for blob in capture["payloads"].values():
            if len(blob) > limits.max_payload_bytes:
                raise BoundExceeded("payload_byte_limit")
            incoming += len(blob)
    if state == "readable" and footprint_bytes(resolved) + incoming > limits.max_store_bytes:
        raise BoundExceeded("store_byte_limit")
    if state == "new":
        resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect_writer(resolved, limits)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        if footprint_bytes(resolved) > limits.max_store_bytes:
            raise BoundExceeded("store_byte_limit")
        for capture in captures:
            _insert_capture(connection, capture)
        for diagnostic in diagnostics:
            _insert_diagnostic(connection, diagnostic)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def open_reader(path: Path | str, *, operational_paths: Sequence[Path | str] = ()) -> sqlite3.Connection:
    resolved = resolve_evidence_path(path, operational_paths=operational_paths)
    if not resolved.exists():
        raise EvidenceStoreError("evidence_store_missing")
    inspect_existing_store_file(resolved)
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != STORE_SCHEMA_VERSION:
        connection.close()
        raise EvidenceStoreError(f"unsupported_store_version:{version}")
    return connection


def load_capture(connection: sqlite3.Connection, capture_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM captures WHERE capture_id = ?",
        (capture_id,),
    ).fetchone()
    if row is None:
        return None
    payloads = connection.execute(
        """
        SELECT role, ordinal, content_hash
        FROM capture_payloads
        WHERE capture_id = ?
        ORDER BY role ASC, ordinal ASC
        """,
        (capture_id,),
    ).fetchall()
    return {
        "row": {key: row[key] for key in row.keys()},
        "refs": [
            {"role": item["role"], "ordinal": item["ordinal"], "content_hash": item["content_hash"]}
            for item in payloads
        ],
    }


def read_payload(connection: sqlite3.Connection, digest: str) -> bytes | None:
    row = connection.execute(
        "SELECT canonical_bytes, byte_length, content_hash FROM payloads WHERE content_hash = ?",
        (digest,),
    ).fetchone()
    if row is None:
        return None
    blob = bytes(row["canonical_bytes"])
    if len(blob) != int(row["byte_length"]) or content_hash(blob) != str(row["content_hash"]):
        raise EvidenceStoreError("payload_bytes_do_not_match_record")
    return blob


def _connect_writer(path: Path, limits: BoundLimits) -> sqlite3.Connection:
    timeout = max(1, int(limits.max_lock_wait_ms)) / 1000
    try:
        connection = sqlite3.connect(path, timeout=timeout)
    except sqlite3.Error as exc:
        raise EvidenceStoreError(f"evidence_store_open_failed:{type(exc).__name__}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {int(limits.max_lock_wait_ms)}")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error as exc:
        connection.close()
        raise EvidenceStoreError(f"evidence_store_journal_failed:{type(exc).__name__}") from exc
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version == STORE_SCHEMA_VERSION:
        row = connection.execute(
            "SELECT meta_value FROM evidence_meta WHERE meta_key = 'format'"
        ).fetchone()
        if row is None or str(row[0]) != STORE_FORMAT:
            raise EvidenceStoreError("evidence_store_format_mismatch")
        return
    if version != 0:
        raise EvidenceStoreError(f"unsupported_store_version:{version}")
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if tables:
        raise EvidenceStoreError("unrelated_sqlite_database_rejected")
    for statement in _SCHEMA.split(";"):
        sql = statement.strip()
        if sql:
            connection.execute(sql)
    connection.execute(
        "INSERT INTO evidence_meta (meta_key, meta_value) VALUES ('format', ?)",
        (STORE_FORMAT,),
    )
    connection.execute(f"PRAGMA user_version = {STORE_SCHEMA_VERSION}")


def _insert_capture(connection: sqlite3.Connection, capture: Mapping[str, Any]) -> None:
    payloads: Mapping[str, bytes] = capture["payloads"]
    hashes: dict[str, str] = {}
    for role, blob in payloads.items():
        if not isinstance(blob, (bytes, bytearray)):
            raise EvidenceStoreError("payload_must_be_bytes")
        digest = content_hash(bytes(blob))
        hashes[str(role)] = digest
        existing = connection.execute(
            "SELECT canonical_bytes FROM payloads WHERE content_hash = ?",
            (digest,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO payloads (content_hash, codec_version, byte_length, canonical_bytes)
                VALUES (?, ?, ?, ?)
                """,
                (digest, CODEC_VERSION, len(blob), sqlite3.Binary(bytes(blob))),
            )
        elif bytes(existing[0]) != bytes(blob):
            raise EvidenceStoreError("payload_hash_collision")
    connection.execute(
        """
        INSERT INTO captures (
            capture_id, recorded_at, caller_path, evidence_lineage, codec_version,
            store_schema_version, application_schema_version, build_git_sha, build_dirty,
            implementation_fingerprint, dependency_versions_json, policy_id, policy_family,
            policy_supported, capture_status, incomplete_reason, transaction_status,
            savepoint_name, savepoint_disposition, enclosing_disposition, exception_class,
            exception_message, reference_count, parent_depth
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            capture["capture_id"],
            capture.get("recorded_at") or datetime.now(UTC).isoformat(),
            capture["caller_path"],
            capture["evidence_lineage"],
            CODEC_VERSION,
            STORE_SCHEMA_VERSION,
            int(capture["application_schema_version"]),
            capture["build_git_sha"],
            capture["build_dirty"],
            capture["implementation_fingerprint"],
            capture["dependency_versions_json"],
            capture["policy_id"],
            capture["policy_family"],
            1 if capture["policy_supported"] else 0,
            capture["capture_status"],
            capture.get("incomplete_reason"),
            capture["transaction_status"],
            capture.get("savepoint_name"),
            capture["savepoint_disposition"],
            capture["enclosing_disposition"],
            capture.get("exception_class"),
            capture.get("exception_message"),
            len(payloads),
            int(capture.get("parent_depth") or 0),
        ),
    )
    for ordinal, role in enumerate(sorted(hashes)):
        connection.execute(
            """
            INSERT INTO capture_payloads (capture_id, role, content_hash, ordinal)
            VALUES (?, ?, ?, ?)
            """,
            (capture["capture_id"], role, hashes[role], ordinal),
        )


def _insert_diagnostic(connection: sqlite3.Connection, diagnostic: Mapping[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO capture_diagnostics (
            diagnostic_id, recorded_at, kind, lifecycle_id, detail, transaction_status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            diagnostic["diagnostic_id"],
            diagnostic.get("recorded_at") or datetime.now(UTC).isoformat(),
            diagnostic["kind"],
            diagnostic.get("lifecycle_id"),
            diagnostic["detail"],
            diagnostic.get("transaction_status") or "commit_unknown",
        ),
    )
