"""Offline reproduction of one captured outcome-evaluator invocation.

Replay reads the evidence store without migrating or writing it, seeds a new
scratch repository with the captured dependency closure, and calls
``evaluate_closed_candle_outcomes``. It does not fetch candles, deliver
messages, or open the default operational database.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.research.durable_source_replay.capture import replay_guard
from app.research.durable_source_replay.codec import CodecError, content_hash, decode_canonical
from app.research.durable_source_replay.bounds import DEFAULT_BOUNDS
from app.research.durable_source_replay.constants import (
    CAPTURE_COMPLETE,
    CAPTURE_STATUS_VALUES,
    CODEC_VERSION,
    EVIDENCE_CORRUPT,
    EVIDENCE_INCOMPLETE,
    EXIT_BY_STATUS,
    IMPLEMENTATION_ATTESTATION_VERSION,
    NONSEMANTIC_EVENT_COLUMNS,
    NONSEMANTIC_PROGRESS_COLUMNS,
    PAYLOAD_CALL,
    PAYLOAD_DELIVERY,
    PAYLOAD_EFFECTS,
    PAYLOAD_POLICY,
    PAYLOAD_PRESTATE,
    PAYLOAD_RESULT,
    REPLAY_MATCH,
    REPLAY_MISMATCH,
    SAVEPOINT_DISPOSITION_VALUES,
    SAVEPOINT_NONE,
    SAVEPOINT_OPEN,
    SAVEPOINT_RELEASED,
    SAVEPOINT_ROLLED_BACK,
    STORE_SCHEMA_VERSION,
    TX_COMMIT_INTERRUPTED,
    TX_COMMIT_UNKNOWN,
    TX_ENCLOSING_COMMITTED,
    TX_ENCLOSING_ROLLED_BACK,
    TX_SAVEPOINT_ROLLED_BACK,
    TX_STATUS_VALUES,
    UNSUPPORTED_FORMAT,
    UNSUPPORTED_IMPLEMENTATION_OR_POLICY,
)
from app.research.durable_source_replay.identity import dependency_versions, implementation_fingerprint
from app.research.durable_source_replay.paths import EvidencePathError, resolve_evidence_path
from app.research.durable_source_replay.prestate import (
    project_evaluation,
    project_exception,
    reconstruct_record,
    seed_prestate,
    semantic_effects,
    snapshot_effects,
)
from app.research.durable_source_replay.store import (
    EvidenceStoreError,
    load_capture,
    open_reader,
    read_payload,
)
from app.research.evaluation_policy import (
    build_runtime_evaluation_policy,
    parse_evaluation_policy_payload,
)
from app.storage.database import SCHEMA_VERSION, connect_database, initialize_database

_REQUIRED_ROLES = (
    PAYLOAD_CALL,
    PAYLOAD_PRESTATE,
    PAYLOAD_DELIVERY,
    PAYLOAD_POLICY,
    PAYLOAD_RESULT,
    PAYLOAD_EFFECTS,
)

COMPARISON_EXCLUSIONS = {
    "setup_lifecycle_outcome_progress.id": (
        "SQLite surrogate. The evaluator selects progress by lifecycle_id and "
        "plan_identity. Comparison sorts on plan_identity."
    ),
    "setup_lifecycle_outcome_progress.created_at": (
        "Insert default CURRENT_TIMESTAMP. The evaluator stores first_evaluated_at."
    ),
    "setup_lifecycle_outcome_progress.updated_at": (
        "Upsert assigns CURRENT_TIMESTAMP. No evaluator predicate reads it."
    ),
    "setup_lifecycle_events.event_id": (
        "AUTOINCREMENT. Pre-existing rows are seeded with their captured ids so "
        "timestamp ties keep read order. Newly inserted ids differ and are excluded. "
        "Comparison keeps timestamp order plus the captured ordinal."
    ),
}


def inspect_capture(
    *,
    evidence_path: Path | str,
    capture_id: str,
    operational_paths: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    loaded = _load(evidence_path, capture_id, operational_paths)
    if loaded["status"] != "ready":
        return _public(loaded)
    row = loaded["row"]
    report = _public(loaded)
    report["status"] = (
        EVIDENCE_INCOMPLETE if row["capture_status"] != CAPTURE_COMPLETE else "INSPECTION_OK"
    )
    report["exit_code"] = (
        EXIT_BY_STATUS[EVIDENCE_INCOMPLETE]
        if report["status"] == EVIDENCE_INCOMPLETE
        else 0
    )
    return report


def replay_capture(
    *,
    evidence_path: Path | str,
    capture_id: str,
    scratch_dir: Path | str,
    operational_paths: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    loaded = _load(evidence_path, capture_id, operational_paths)
    if loaded["status"] != "ready":
        return _public(loaded)
    compatibility = _compatibility(loaded)
    if compatibility is not None:
        loaded["status"] = compatibility[0]
        loaded["reason"] = compatibility[1]
        return _public(loaded)
    try:
        scratch = _scratch_database(
            scratch_dir,
            evidence_path,
            operational_paths=operational_paths,
        )
    except EvidencePathError as exc:
        loaded["status"] = "USAGE"
        loaded["reason"] = exc.reason
        return _public(loaded)
    try:
        return _run(loaded, scratch)
    except Exception as exc:
        loaded["status"] = EVIDENCE_CORRUPT
        loaded["reason"] = f"replay_failed:{type(exc).__name__}:{exc}"
        return _public(loaded)


def _run(loaded: dict[str, Any], scratch: Path) -> dict[str, Any]:
    call = loaded["payloads"][PAYLOAD_CALL]
    prestate = loaded["payloads"][PAYLOAD_PRESTATE]
    connection = connect_database(scratch)
    try:
        initialize_database(connection)
        connection.execute("BEGIN IMMEDIATE")
        seed_prestate(connection, prestate)
        repository = SQLiteSetupLifecycleRepository.__new__(SQLiteSetupLifecycleRepository)
        repository.database_path = scratch
        repository.expected_epoch_id = None
        repository.expected_identity = None
        repository.connection = connection
        candles = _candles(call["execution_candles"])
        record = reconstruct_record(call["record"])
        with replay_guard():
            try:
                result = evaluate_closed_candle_outcomes(
                    record,
                    execution_candles=candles,
                    execution_timeframe=call["execution_timeframe"],
                    decision_timestamp=call["decision_timestamp"],
                    evaluated_at=call["evaluated_at"],
                    repository=repository,
                    scan_run_id=call["scan_run_id"],
                )
                replay_exception = None
            except Exception as exc:
                result = None
                replay_exception = exc
        effects = snapshot_effects(
            connection,
            lifecycle_id=str(prestate["lifecycle_id"]),
            limits=DEFAULT_BOUNDS,
        )
        connection.commit()
    finally:
        connection.close()
    stored_result = loaded["payloads"][PAYLOAD_RESULT]
    mismatches: list[str] = []
    stored_exception = stored_result.get("exception")
    if stored_exception is None:
        if replay_exception is not None:
            mismatches.append("replay_raised:" + type(replay_exception).__name__)
        elif result is not None:
            expected = stored_result.get("evaluation")
            actual = project_evaluation(result)
            if expected != actual:
                mismatches.append("evaluation_mismatch")
                loaded["mismatch_detail"] = _brief_diff(expected, actual)
    else:
        if replay_exception is None:
            mismatches.append("replay_did_not_raise")
        else:
            actual_exception = project_exception(replay_exception)
            if actual_exception != stored_exception:
                mismatches.append("exception_mismatch")
    if semantic_effects(effects) != semantic_effects(loaded["payloads"][PAYLOAD_EFFECTS]):
        mismatches.append("effects_mismatch")
        loaded["effects_detail"] = _brief_diff(
            semantic_effects(loaded["payloads"][PAYLOAD_EFFECTS]),
            semantic_effects(effects),
        )
    status = REPLAY_MISMATCH if mismatches else REPLAY_MATCH
    loaded["status"] = status
    loaded["reason"] = None if not mismatches else ",".join(mismatches)
    loaded["mismatches"] = mismatches
    return _public(loaded)


def _compatibility(loaded: dict[str, Any]) -> tuple[str, str] | None:
    row = loaded["row"]
    if row["capture_status"] != CAPTURE_COMPLETE:
        return EVIDENCE_INCOMPLETE, str(row["incomplete_reason"] or "capture_incomplete")
    if int(row["application_schema_version"]) != SCHEMA_VERSION:
        return UNSUPPORTED_IMPLEMENTATION_OR_POLICY, "application_schema_mismatch"
    if str(row["codec_version"]) != CODEC_VERSION:
        return UNSUPPORTED_FORMAT, "codec_version_mismatch"
    deps = loaded.get("dependency_versions") or {}
    attestation = deps.get("implementation_attestation_version")
    if attestation != IMPLEMENTATION_ATTESTATION_VERSION:
        return (
            UNSUPPORTED_IMPLEMENTATION_OR_POLICY,
            f"unsupported_implementation_attestation:{attestation!s}",
        )
    if str(row["implementation_fingerprint"]) != implementation_fingerprint():
        return UNSUPPORTED_IMPLEMENTATION_OR_POLICY, "implementation_fingerprint_mismatch"
    try:
        recorded_versions = json.loads(row["dependency_versions_json"])
    except json.JSONDecodeError:
        return EVIDENCE_CORRUPT, "dependency_versions_invalid"
    if recorded_versions != dependency_versions():
        return UNSUPPORTED_IMPLEMENTATION_OR_POLICY, "dependency_version_mismatch"
    policy = loaded["payloads"][PAYLOAD_POLICY]
    if not policy.get("supported") or not policy.get("canonical_text"):
        return UNSUPPORTED_IMPLEMENTATION_OR_POLICY, str(policy.get("reason") or "policy_unsupported")
    try:
        parsed = parse_evaluation_policy_payload(json.loads(policy["canonical_text"]))
    except Exception as exc:
        return EVIDENCE_CORRUPT, f"policy_manifest_invalid:{type(exc).__name__}"
    if parsed.policy_id != policy.get("policy_id"):
        return EVIDENCE_CORRUPT, "policy_id_does_not_match_canonical_bytes"
    if parsed.canonical_bytes.decode("utf-8") != policy["canonical_text"]:
        return EVIDENCE_CORRUPT, "policy_canonical_bytes_mismatch"
    call = loaded["payloads"][PAYLOAD_CALL]
    try:
        current = build_runtime_evaluation_policy(
            execution_timeframe=str(call["execution_timeframe"])
        )
    except Exception as exc:
        return UNSUPPORTED_IMPLEMENTATION_OR_POLICY, f"current_policy_unsupported:{type(exc).__name__}"
    if current.canonical_bytes.decode("utf-8") != policy["canonical_text"]:
        return UNSUPPORTED_IMPLEMENTATION_OR_POLICY, "policy_bytes_differ_from_executing_implementation"
    if current.family != policy.get("family"):
        return UNSUPPORTED_IMPLEMENTATION_OR_POLICY, "policy_family_mismatch"
    return None


def _validate_envelope(row: Mapping[str, Any], refs: Sequence[Mapping[str, Any]], payloads: Mapping[str, Any]) -> str | None:
    """Return a corrupt/unsupported reason, or None when the occurrence envelope is consistent."""

    if int(row.get("store_schema_version") or -1) != STORE_SCHEMA_VERSION:
        return f"store_schema_version_mismatch:{row.get('store_schema_version')}"
    if str(row.get("codec_version") or "") != CODEC_VERSION:
        return f"capture_codec_version_mismatch:{row.get('codec_version')}"
    capture_status = str(row.get("capture_status") or "")
    if capture_status not in CAPTURE_STATUS_VALUES:
        return f"unknown_capture_status:{capture_status}"
    tx_status = str(row.get("transaction_status") or "")
    if tx_status not in TX_STATUS_VALUES:
        return f"unknown_transaction_status:{tx_status}"
    enclosing = str(row.get("enclosing_disposition") or "")
    if enclosing not in TX_STATUS_VALUES:
        return f"unknown_enclosing_disposition:{enclosing}"
    savepoint = str(row.get("savepoint_disposition") or "")
    if savepoint not in SAVEPOINT_DISPOSITION_VALUES:
        return f"unknown_savepoint_disposition:{savepoint}"
    if tx_status == TX_ENCLOSING_COMMITTED and enclosing != TX_ENCLOSING_COMMITTED:
        return "transaction_enclosing_inconsistent"
    if enclosing == TX_ENCLOSING_ROLLED_BACK and tx_status == TX_ENCLOSING_COMMITTED:
        return "transaction_enclosing_inconsistent"
    if enclosing == TX_COMMIT_INTERRUPTED and tx_status == TX_ENCLOSING_COMMITTED:
        return "transaction_enclosing_inconsistent"
    if tx_status == TX_SAVEPOINT_ROLLED_BACK and savepoint != SAVEPOINT_ROLLED_BACK:
        return "savepoint_transaction_inconsistent"
    if int(row.get("reference_count") or -1) != len(refs):
        return "reference_count_mismatch"
    roles = [str(item["role"]) for item in refs]
    if len(roles) != len(set(roles)):
        return "duplicate_payload_role"
    ordinals = [int(item["ordinal"]) for item in refs]
    if ordinals != list(range(len(ordinals))):
        return "payload_ordinal_mismatch"
    missing = [role for role in _REQUIRED_ROLES if role not in payloads]
    if missing:
        return "missing_reference:" + ",".join(missing)
    call = payloads.get(PAYLOAD_CALL)
    policy = payloads.get(PAYLOAD_POLICY)
    if not isinstance(call, dict):
        return "call_payload_shape_invalid"
    if not isinstance(policy, dict):
        return "policy_payload_shape_invalid"
    if str(call.get("caller_path") or "") != str(row.get("caller_path") or ""):
        return "caller_path_binding_mismatch"
    if str(call.get("evidence_lineage") or "") != str(row.get("evidence_lineage") or ""):
        return "evidence_lineage_binding_mismatch"
    if str(policy.get("policy_id") or "") != str(row.get("policy_id") or ""):
        return "policy_id_header_mismatch"
    if str(policy.get("family") or "") != str(row.get("policy_family") or ""):
        return "policy_family_header_mismatch"
    supported_flag = bool(int(row.get("policy_supported") or 0))
    if bool(policy.get("supported")) != supported_flag:
        return "policy_supported_header_mismatch"
    return None


def _load(
    evidence_path: Path | str,
    capture_id: str,
    operational_paths: tuple[Path | str, ...],
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "capture_id": capture_id,
        "status": EVIDENCE_INCOMPLETE,
        "reason": "capture_not_found",
        "transaction_status": None,
        "capture_status": None,
        "caller_path": None,
        "evidence_lineage": None,
        "row": None,
        "payloads": {},
        "dependency_versions": {},
        "mismatches": [],
    }
    try:
        connection = open_reader(evidence_path, operational_paths=operational_paths)
    except EvidencePathError as exc:
        base["status"] = UNSUPPORTED_FORMAT if "unsupported_store_version" in exc.reason else EVIDENCE_CORRUPT
        if exc.reason == "evidence_store_missing":
            base["status"] = EVIDENCE_INCOMPLETE
        base["reason"] = exc.reason
        return base
    except EvidenceStoreError as exc:
        reason = exc.reason
        if reason.startswith("unsupported_store_version"):
            base["status"] = UNSUPPORTED_FORMAT
        elif reason == "evidence_store_missing":
            base["status"] = EVIDENCE_INCOMPLETE
        else:
            base["status"] = EVIDENCE_CORRUPT
        base["reason"] = reason
        return base
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != STORE_SCHEMA_VERSION:
            base["status"] = UNSUPPORTED_FORMAT
            base["reason"] = f"unsupported_store_version:{version}"
            return base
        loaded = load_capture(connection, capture_id)
        if loaded is None:
            base["reason"] = "capture_not_found"
            return base
        row = loaded["row"]
        base["row"] = row
        base["transaction_status"] = row["transaction_status"]
        base["capture_status"] = row["capture_status"]
        base["caller_path"] = row["caller_path"]
        base["evidence_lineage"] = row["evidence_lineage"]
        try:
            base["dependency_versions"] = json.loads(row["dependency_versions_json"] or "{}")
        except json.JSONDecodeError:
            base["status"] = EVIDENCE_CORRUPT
            base["reason"] = "dependency_versions_invalid"
            return base
        decoded: dict[str, Any] = {}
        decode_budget = DEFAULT_BOUNDS.max_decode_bytes
        decoded_bytes = 0
        for ref in loaded["refs"]:
            try:
                payload = read_payload(connection, ref["content_hash"])
            except EvidenceStoreError as exc:
                base["status"] = EVIDENCE_CORRUPT
                base["reason"] = exc.reason
                return base
            if payload is None:
                base["status"] = EVIDENCE_CORRUPT
                base["reason"] = f"missing_payload:{ref['role']}"
                return base
            blob = payload["bytes"]
            if payload["codec_version"] != CODEC_VERSION:
                base["status"] = UNSUPPORTED_FORMAT
                base["reason"] = f"payload_codec_version_mismatch:{ref['role']}"
                return base
            if content_hash(blob) != ref["content_hash"] or len(blob) == 0:
                base["status"] = EVIDENCE_CORRUPT
                base["reason"] = f"payload_integrity:{ref['role']}"
                return base
            decoded_bytes += len(blob)
            if decoded_bytes > decode_budget:
                base["status"] = EVIDENCE_CORRUPT
                base["reason"] = "decode_byte_limit"
                return base
            try:
                decoded[ref["role"]] = decode_canonical(blob)
            except CodecError as exc:
                base["status"] = EVIDENCE_CORRUPT
                base["reason"] = f"payload_decode:{ref['role']}:{exc.reason}"
                return base
        envelope_error = _validate_envelope(row, loaded["refs"], decoded)
        if envelope_error is not None:
            if envelope_error.startswith("unsupported") or "codec_version" in envelope_error:
                base["status"] = UNSUPPORTED_FORMAT
            elif envelope_error.startswith("store_schema"):
                base["status"] = UNSUPPORTED_FORMAT
            else:
                base["status"] = EVIDENCE_CORRUPT
            base["reason"] = envelope_error
            # Do not expose mutated conflicting metadata as a positive claim.
            base["transaction_status"] = row["transaction_status"]
            base["payloads"] = {}
            return base
        base["payloads"] = decoded
        base["status"] = "ready"
        base["reason"] = None
        return base
    except sqlite3.Error as exc:
        base["status"] = EVIDENCE_CORRUPT
        base["reason"] = f"evidence_read_failed:{type(exc).__name__}"
        return base
    finally:
        connection.close()


def _scratch_database(
    scratch_dir: Path | str,
    evidence_path: Path | str,
    *,
    operational_paths: tuple[Path | str, ...],
) -> Path:
    directory = Path(scratch_dir).expanduser().resolve(strict=False)
    if not directory.is_dir():
        raise EvidencePathError("scratch_directory_required")
    database = directory / "replay_operational.sqlite"
    if database.exists():
        raise EvidencePathError("scratch_database_must_be_new")
    evidence = resolve_evidence_path(evidence_path, operational_paths=operational_paths)
    resolved = resolve_evidence_path(database, operational_paths=(*operational_paths, evidence))
    if resolved == evidence:
        raise EvidencePathError("scratch_aliases_evidence_store")
    return resolved


def _brief_diff(expected: Any, actual: Any, *, path: str = "$", found: list[str] | None = None) -> list[str]:
    if found is None:
        found = []
    if len(found) >= 12:
        return found
    if type(expected) is not type(actual):
        found.append(f"{path}:type {type(expected).__name__}!={type(actual).__name__}")
        return found
    if isinstance(expected, dict):
        keys = list(dict.fromkeys([*expected.keys(), *actual.keys()]))
        for key in keys:
            if key not in expected or key not in actual or expected[key] != actual[key]:
                _brief_diff(expected.get(key), actual.get(key), path=f"{path}.{key}", found=found)
            if len(found) >= 12:
                break
        return found
    if isinstance(expected, list):
        if len(expected) != len(actual):
            found.append(f"{path}:len {len(expected)}!={len(actual)}")
        for index, (left, right) in enumerate(zip(expected, actual)):
            if left != right:
                _brief_diff(left, right, path=f"{path}[{index}]", found=found)
            if len(found) >= 12:
                break
        return found
    if expected != actual:
        found.append(f"{path}:{expected!r}!={actual!r}"[:240])
    return found


def _candles(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise EvidenceStoreError("candles_not_a_list")
    restored: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or "fields" not in item:
            raise EvidenceStoreError("candle_shape_unsupported")
        candle: dict[str, Any] = {}
        for pair in item["fields"]:
            key = pair["k"]
            if key in candle:
                raise EvidenceStoreError("duplicate_candle_field")
            candle[str(key)] = pair["v"]
        restored.append(candle)
    return restored


def _public(loaded: dict[str, Any]) -> dict[str, Any]:
    status = loaded["status"] if loaded["status"] != "ready" else EVIDENCE_INCOMPLETE
    if status == "INSPECTION_OK":
        exit_code = 0
    else:
        exit_code = EXIT_BY_STATUS.get(status, 1)
    return {
        "capture_id": loaded["capture_id"],
        "status": status,
        "reason": loaded.get("reason"),
        "exit_code": exit_code,
        "transaction_status": loaded.get("transaction_status"),
        "capture_status": loaded.get("capture_status"),
        "caller_path": loaded.get("caller_path"),
        "evidence_lineage": loaded.get("evidence_lineage"),
        "mismatches": list(loaded.get("mismatches") or []),
        "mismatch_detail": loaded.get("mismatch_detail"),
        "effects_detail": loaded.get("effects_detail"),
        "comparison_exclusions": COMPARISON_EXCLUSIONS,
        "nonsemantic_progress_columns": list(NONSEMANTIC_PROGRESS_COLUMNS),
        "nonsemantic_event_columns": list(NONSEMANTIC_EVENT_COLUMNS),
        "claims": {
            "computation_replay": status == REPLAY_MATCH,
            "local_delivery_provenance": _local_delivery_provenance(loaded),
            "operational_persistence": (
                status == REPLAY_MATCH and loaded.get("transaction_status") == TX_ENCLOSING_COMMITTED
            ),
            "authenticated_venue_evidence": False,
            "possession_before_decision_cutoff": False,
            "expectancy": False,
            "admission_or_episode": False,
        },
    }


def _local_delivery_provenance(loaded: dict[str, Any]) -> bool:
    """True only for a complete captured envelope with a matched handoff.

    A missing owner-monitoring envelope, a truncated parent walk, an incomplete
    lineage flag, or a mismatched handoff stays false. This is not venue proof.
    """

    delivery = loaded.get("payloads", {}).get(PAYLOAD_DELIVERY)
    if not isinstance(delivery, dict) or delivery.get("truncated"):
        return False
    node = delivery.get("delivery")
    handoff = delivery.get("handoff")
    if not isinstance(node, dict) or node.get("kind") != "candle_batch_delivery":
        return False
    if not node.get("lineage_complete"):
        return False
    if not isinstance(handoff, dict) or handoff.get("kind") != "batch_handoff":
        return False
    return handoff.get("disposition") == "matched"
