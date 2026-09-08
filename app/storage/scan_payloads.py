from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.storage.database import StorageError

INLINE_V1 = "inline_v1"
SYMBOL_REFS_V1 = "symbol_refs_v1"
SUPPORTED_FORMATS = frozenset({INLINE_V1, SYMBOL_REFS_V1})
REFERENCE_KEYS = frozenset({"symbol", "sha256"})

FALLBACK_INLINE_REQUESTED = "inline_requested"
FALLBACK_RESULTS_MISSING = "results_missing"
FALLBACK_RESULTS_EMPTY = "results_empty"
FALLBACK_RESULTS_NOT_LIST = "results_not_list"
FALLBACK_INVALID_SYMBOL = "invalid_symbol"
FALLBACK_DUPLICATE_SYMBOLS = "duplicate_symbols"
FALLBACK_SYMBOL_SET_MISMATCH = "symbol_set_mismatch"
FALLBACK_PAYLOAD_CHILD_BYTES_MISMATCH = "payload_child_bytes_mismatch"
FALLBACK_NO_POSITIVE_SAVINGS = "no_positive_savings"
ELIGIBLE_SYMBOL_REFS = "symbol_refs_eligible"


class ScanPayloadIntegrityError(StorageError):
    """Raised when a stored scan payload cannot be reconstructed losslessly."""


@dataclass(frozen=True)
class EncodedScanPayload:
    format: str
    physical_payload: dict[str, Any]
    physical_json: str
    reason: str
    inline_utf8_bytes: int
    physical_utf8_bytes: int


@dataclass(frozen=True)
class PhysicalScanPayloadInspection:
    run_id: str
    encoding: str
    raw_payload_utf8_bytes: int
    child_raw_utf8_bytes: int
    reference_count: int | None
    note: str


def encode_scan_raw_payload(
    logical_payload: Mapping[str, Any],
    symbol_records: Sequence[Any],
    *,
    dumps: Callable[[Any], str],
    inline_only: bool = False,
) -> EncodedScanPayload:
    """Return the physical scan payload. Summaries must already have been computed from logical_payload."""

    inline_json = dumps(logical_payload)
    inline_bytes = _utf8_len(inline_json)
    if inline_only:
        return EncodedScanPayload(
            format=INLINE_V1,
            physical_payload=dict(logical_payload),
            physical_json=inline_json,
            reason=FALLBACK_INLINE_REQUESTED,
            inline_utf8_bytes=inline_bytes,
            physical_utf8_bytes=inline_bytes,
        )

    eligible, compact_payload, reason = _try_symbol_refs(logical_payload, symbol_records, dumps=dumps)
    if not eligible or compact_payload is None:
        return EncodedScanPayload(
            format=INLINE_V1,
            physical_payload=dict(logical_payload),
            physical_json=inline_json,
            reason=reason,
            inline_utf8_bytes=inline_bytes,
            physical_utf8_bytes=inline_bytes,
        )

    compact_json = dumps(compact_payload)
    compact_bytes = _utf8_len(compact_json)
    if compact_bytes >= inline_bytes:
        return EncodedScanPayload(
            format=INLINE_V1,
            physical_payload=dict(logical_payload),
            physical_json=inline_json,
            reason=FALLBACK_NO_POSITIVE_SAVINGS,
            inline_utf8_bytes=inline_bytes,
            physical_utf8_bytes=inline_bytes,
        )
    return EncodedScanPayload(
        format=SYMBOL_REFS_V1,
        physical_payload=compact_payload,
        physical_json=compact_json,
        reason=ELIGIBLE_SYMBOL_REFS,
        inline_utf8_bytes=inline_bytes,
        physical_utf8_bytes=compact_bytes,
    )


def load_logical_scan_payload(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Load one scan's full logical payload from a caller-owned read snapshot."""

    started_snapshot = _ensure_read_snapshot(connection)
    try:
        columns = _table_columns(connection, "scan_runs")
        if "raw_payload_json" not in columns:
            raise ScanPayloadIntegrityError(f"scan_runs.raw_payload_json is unavailable for run_id={run_id}")
        has_format = "raw_payload_format" in columns
        if has_format:
            cursor = connection.execute(
                """
                SELECT raw_payload_json, raw_payload_format
                FROM scan_runs
                WHERE run_id = ?
                """,
                (run_id,),
            )
        else:
            cursor = connection.execute(
                "SELECT raw_payload_json FROM scan_runs WHERE run_id = ?",
                (run_id,),
            )
        row = cursor.fetchone()
        if row is None:
            raise ScanPayloadIntegrityError(f"scan run was not found: {run_id}")
        mapping = _row_mapping(row, cursor.description)
        raw_text = mapping.get("raw_payload_json")
        if not isinstance(raw_text, str):
            raise ScanPayloadIntegrityError(f"scan run raw_payload_json is not text for run_id={run_id}")
        if has_format:
            format_name = mapping.get("raw_payload_format")
            if not isinstance(format_name, str) or not format_name.strip():
                raise ScanPayloadIntegrityError(
                    f"scan run raw_payload_format is required when the column exists; run_id={run_id}"
                )
        else:
            format_name = INLINE_V1
        return _hydrate_logical_payload(connection, run_id, raw_text, format_name)
    finally:
        if started_snapshot:
            _end_read_snapshot(connection)


def inspect_physical_scan_payload(connection: sqlite3.Connection, run_id: str) -> PhysicalScanPayloadInspection:
    """Labeled physical-storage diagnostic. This is not the logical payload."""

    columns = _table_columns(connection, "scan_runs")
    has_format = "raw_payload_format" in columns
    if has_format:
        cursor = connection.execute(
            "SELECT raw_payload_json, raw_payload_format FROM scan_runs WHERE run_id = ?",
            (run_id,),
        )
    else:
        cursor = connection.execute(
            "SELECT raw_payload_json FROM scan_runs WHERE run_id = ?",
            (run_id,),
        )
    row = cursor.fetchone()
    if row is None:
        raise ScanPayloadIntegrityError(f"scan run was not found: {run_id}")
    mapping = _row_mapping(row, cursor.description)
    raw_text = mapping.get("raw_payload_json")
    if not isinstance(raw_text, str):
        raise ScanPayloadIntegrityError(f"scan run raw_payload_json is not text for run_id={run_id}")
    encoding = mapping.get("raw_payload_format") if has_format else INLINE_V1
    if not isinstance(encoding, str) or not encoding:
        encoding = INLINE_V1
    child_cursor = connection.execute(
        "SELECT raw_result_json FROM symbol_results WHERE run_id = ?",
        (run_id,),
    )
    children = child_cursor.fetchall()
    child_bytes = 0
    for child in children:
        child_mapping = _row_mapping(child, child_cursor.description)
        child_text = child_mapping.get("raw_result_json")
        if isinstance(child_text, str):
            child_bytes += _utf8_len(child_text)
    reference_count = None
    if encoding == SYMBOL_REFS_V1:
        loaded = _parse_object(raw_text, run_id)
        results = loaded.get("results")
        reference_count = len(results) if isinstance(results, list) else None
    return PhysicalScanPayloadInspection(
        run_id=run_id,
        encoding=encoding,
        raw_payload_utf8_bytes=_utf8_len(raw_text),
        child_raw_utf8_bytes=child_bytes,
        reference_count=reference_count,
        note=(
            "Physical scan_runs.raw_payload_json encoding and UTF-8 sizes; "
            "use load_logical_scan_payload for the reconstructed logical payload."
        ),
    )


def sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _try_symbol_refs(
    logical_payload: Mapping[str, Any],
    symbol_records: Sequence[Any],
    *,
    dumps: Callable[[Any], str],
) -> tuple[bool, dict[str, Any] | None, str]:
    if "results" not in logical_payload:
        return False, None, FALLBACK_RESULTS_MISSING
    results = logical_payload.get("results")
    if not isinstance(results, list):
        return False, None, FALLBACK_RESULTS_NOT_LIST
    if not results:
        return False, None, FALLBACK_RESULTS_EMPTY

    payload_symbols: list[str] = []
    for item in results:
        if not isinstance(item, Mapping):
            return False, None, FALLBACK_INVALID_SYMBOL
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or symbol == "":
            return False, None, FALLBACK_INVALID_SYMBOL
        payload_symbols.append(symbol)
    if len(set(payload_symbols)) != len(payload_symbols):
        return False, None, FALLBACK_DUPLICATE_SYMBOLS

    records_by_symbol: dict[str, Any] = {}
    for record in symbol_records:
        symbol = getattr(record, "symbol", None)
        if not isinstance(symbol, str) or symbol == "":
            return False, None, FALLBACK_INVALID_SYMBOL
        if symbol in records_by_symbol:
            return False, None, FALLBACK_DUPLICATE_SYMBOLS
        records_by_symbol[symbol] = record
    if set(payload_symbols) != set(records_by_symbol):
        return False, None, FALLBACK_SYMBOL_SET_MISMATCH

    references: list[dict[str, str]] = []
    for item, symbol in zip(results, payload_symbols, strict=True):
        record = records_by_symbol[symbol]
        raw_result_json = getattr(record, "raw_result_json")
        if not isinstance(raw_result_json, str):
            return False, None, FALLBACK_PAYLOAD_CHILD_BYTES_MISMATCH
        if dumps(item) != raw_result_json:
            return False, None, FALLBACK_PAYLOAD_CHILD_BYTES_MISMATCH
        references.append({"symbol": symbol, "sha256": sha256_utf8(raw_result_json)})

    compact = dict(logical_payload)
    compact["results"] = references
    return True, compact, ELIGIBLE_SYMBOL_REFS


def _hydrate_logical_payload(
    connection: sqlite3.Connection,
    run_id: str,
    raw_text: str,
    format_name: str,
) -> dict[str, Any]:
    payload = _parse_object(raw_text, run_id)
    if format_name == INLINE_V1:
        return payload
    if format_name not in SUPPORTED_FORMATS:
        raise ScanPayloadIntegrityError(
            f"unsupported scan raw_payload_format {format_name!r} for run_id={run_id}"
        )
    return _hydrate_symbol_refs(connection, run_id, payload)


def _hydrate_symbol_refs(
    connection: sqlite3.Connection,
    run_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise ScanPayloadIntegrityError(
            f"symbol_refs_v1 results must be a nonempty list for run_id={run_id}"
        )

    references: list[tuple[str, str]] = []
    seen_symbols: set[str] = set()
    for index, item in enumerate(results):
        if not isinstance(item, Mapping) or set(item.keys()) != REFERENCE_KEYS:
            raise ScanPayloadIntegrityError(
                f"symbol_refs_v1 reference {index} is malformed for run_id={run_id}"
            )
        symbol = item.get("symbol")
        digest = item.get("sha256")
        if not isinstance(symbol, str) or symbol == "" or not isinstance(digest, str) or digest == "":
            raise ScanPayloadIntegrityError(
                f"symbol_refs_v1 reference {index} is malformed for run_id={run_id}"
            )
        if symbol in seen_symbols:
            raise ScanPayloadIntegrityError(
                f"symbol_refs_v1 duplicate reference symbol {symbol!r} for run_id={run_id}"
            )
        seen_symbols.add(symbol)
        references.append((symbol, digest))

    child_cursor = connection.execute(
        "SELECT symbol, raw_result_json FROM symbol_results WHERE run_id = ?",
        (run_id,),
    )
    child_rows = child_cursor.fetchall()
    children: dict[str, str] = {}
    for row in child_rows:
        mapping = _row_mapping(row, child_cursor.description)
        symbol = mapping.get("symbol")
        raw_result_json = mapping.get("raw_result_json")
        if not isinstance(symbol, str) or not isinstance(raw_result_json, str):
            raise ScanPayloadIntegrityError(
                f"symbol_refs_v1 child row is malformed for run_id={run_id}"
            )
        if symbol in children:
            raise ScanPayloadIntegrityError(
                f"symbol_refs_v1 duplicate child symbol {symbol!r} for run_id={run_id}"
            )
        children[symbol] = raw_result_json

    if set(children) != seen_symbols:
        raise ScanPayloadIntegrityError(
            f"symbol_refs_v1 child membership does not match references for run_id={run_id}"
        )

    hydrated_results: list[Any] = []
    for symbol, digest in references:
        raw_result_json = children[symbol]
        actual = sha256_utf8(raw_result_json)
        if actual != digest:
            raise ScanPayloadIntegrityError(
                f"symbol_refs_v1 hash mismatch for symbol {symbol!r} run_id={run_id}"
            )
        try:
            loaded = json.loads(raw_result_json)
        except json.JSONDecodeError as exc:
            raise ScanPayloadIntegrityError(
                f"symbol_refs_v1 child JSON is malformed for symbol {symbol!r} run_id={run_id}"
            ) from exc
        hydrated_results.append(loaded)

    logical = dict(payload)
    logical["results"] = hydrated_results
    return logical


def _parse_object(raw_text: str, run_id: str) -> dict[str, Any]:
    try:
        loaded = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ScanPayloadIntegrityError(f"scan raw_payload_json is not valid JSON for run_id={run_id}") from exc
    if not isinstance(loaded, dict):
        raise ScanPayloadIntegrityError(f"scan raw_payload_json must be a JSON object for run_id={run_id}")
    return loaded


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _row_mapping(row: Any, description: Any = None) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {str(key): row[key] for key in keys()}
    if description is not None:
        return {str(column[0]): value for column, value in zip(description, row, strict=True)}
    raise ScanPayloadIntegrityError("scan payload row could not be mapped")


def _ensure_read_snapshot(connection: sqlite3.Connection) -> bool:
    if connection.in_transaction:
        return False
    connection.execute("BEGIN")
    connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    return True


def _end_read_snapshot(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("COMMIT")
    except sqlite3.Error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            return


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


__all__ = [
    "ELIGIBLE_SYMBOL_REFS",
    "EncodedScanPayload",
    "FALLBACK_DUPLICATE_SYMBOLS",
    "FALLBACK_INLINE_REQUESTED",
    "FALLBACK_INVALID_SYMBOL",
    "FALLBACK_NO_POSITIVE_SAVINGS",
    "FALLBACK_PAYLOAD_CHILD_BYTES_MISMATCH",
    "FALLBACK_RESULTS_EMPTY",
    "FALLBACK_RESULTS_MISSING",
    "FALLBACK_RESULTS_NOT_LIST",
    "FALLBACK_SYMBOL_SET_MISMATCH",
    "INLINE_V1",
    "PhysicalScanPayloadInspection",
    "REFERENCE_KEYS",
    "SUPPORTED_FORMATS",
    "SYMBOL_REFS_V1",
    "ScanPayloadIntegrityError",
    "encode_scan_raw_payload",
    "inspect_physical_scan_payload",
    "load_logical_scan_payload",
    "sha256_utf8",
]
