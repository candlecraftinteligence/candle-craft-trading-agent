"""Optional capture around production outcome-evaluator calls.

Capture is off unless an explicit evidence path is installed. Collection inside
the operational transaction is limited to bounded in-memory copies. The evidence
store is written once, after the enclosing transaction disposition is known and
the operational connection has been closed. A capture failure never replaces the
evaluator result or exception.
"""

from __future__ import annotations

import json
import os
import secrets
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.data.candle_integrity import normalize_utc_timestamp
from app.research.durable_source_replay.bounds import DEFAULT_BOUNDS, BoundExceeded, BoundLimits
from app.research.durable_source_replay.codec import CodecError, encode_canonical
from app.research.durable_source_replay.constants import (
    CALLER_LIFECYCLE_SERVICE,
    CALLER_OWNER_MONITORING,
    CAPTURE_COMPLETE,
    CAPTURE_INCOMPLETE,
    PAYLOAD_CALL,
    PAYLOAD_DELIVERY,
    PAYLOAD_EFFECTS,
    PAYLOAD_POLICY,
    PAYLOAD_PRESTATE,
    PAYLOAD_RESULT,
    SAVEPOINT_NONE,
    SAVEPOINT_OPEN,
    SAVEPOINT_RELEASED,
    SAVEPOINT_ROLLED_BACK,
    TX_COMMIT_INTERRUPTED,
    TX_COMMIT_UNKNOWN,
    TX_ENCLOSING_COMMITTED,
    TX_ENCLOSING_ROLLED_BACK,
    TX_SAVEPOINT_ROLLED_BACK,
)
from app.research.durable_source_replay.delivery import serialize_delivery
from app.research.durable_source_replay.identity import (
    build_identity,
    dependency_versions,
    implementation_fingerprint,
)
from app.research.durable_source_replay.prestate import (
    _model_dict,
    project_evaluation,
    project_exception,
    snapshot_dependency_closure,
    snapshot_effects,
)
from app.research.durable_source_replay.store import write_bundle
from app.storage.database import SCHEMA_VERSION

_override: ContextVar["CaptureConfig | None"] = ContextVar("durable_source_replay_override", default=None)
_bounds_override: ContextVar[BoundLimits | None] = ContextVar("durable_source_replay_bounds", default=None)
_replay_guard: ContextVar[bool] = ContextVar("durable_source_replay_guard", default=False)
_sessions: dict[int, "_Session"] = {}
_process_diagnostics: list[dict[str, Any]] = []
_counters: Counter[str] = Counter()
_failures: list[dict[str, str]] = []
_failure_details_truncated = False
_env_config: "CaptureConfig | None" = None
_env_loaded = False
_identity: dict[str, Any] | None = None
_identity_failed = False


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    enabled: bool
    evidence_path: Path | None
    operational_paths: tuple[Path, ...] = ()
    disabled_reason: str | None = None


@dataclass
class _Pending:
    capture_id: str
    savepoint_name: str | None
    savepoint_disposition: str
    fields: dict[str, Any]
    payloads: dict[str, bytes]
    effects_discarded: bool = False
    effects_retained: bool = False


@dataclass
class _Session:
    stack: list[str] = field(default_factory=list)
    pending: list[_Pending] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    enclosing_open: bool = False
    retaining_commit_observed: bool = False
    rollback_observed: bool = False
    instrumented: bool = False


def active_config() -> CaptureConfig | None:
    try:
        override = _override.get()
        if override is not None:
            return override
        _load_env_once()
        return _env_config
    except Exception as exc:
        _remember_failure("active_config_failed", exc)
        return CaptureConfig(enabled=False, evidence_path=None, disabled_reason="config_init_failed")


def active_bounds() -> BoundLimits:
    override = _bounds_override.get()
    return override if override is not None else DEFAULT_BOUNDS


def capture_enabled() -> bool:
    try:
        if _replay_guard.get():
            return False
        config = active_config()
        return bool(config and config.enabled and config.evidence_path is not None)
    except Exception as exc:
        _remember_failure("capture_enabled_failed", exc)
        return False


def capture_counters() -> dict[str, int]:
    result = dict(_counters)
    if _failure_details_truncated:
        result["failure_details_truncated"] = 1
    return result


def capture_failures() -> tuple[dict[str, str], ...]:
    return tuple(_failures)


@contextmanager
def use_capture(config: CaptureConfig, *, bounds: BoundLimits | None = None):
    """Install capture for the current context. The default remains off."""

    if config.enabled:
        try:
            _remember_identity()
        except Exception as exc:
            _remember_failure("identity_init_failed", exc)
    token = _override.set(config)
    bounds_token = _bounds_override.set(bounds)
    try:
        yield
    finally:
        _override.reset(token)
        _bounds_override.reset(bounds_token)


@contextmanager
def replay_guard():
    token = _replay_guard.set(True)
    try:
        yield
    finally:
        _replay_guard.reset(token)


def reset_capture_process_state() -> None:
    """Test helper. Does not enable capture."""

    global _env_config, _env_loaded, _identity, _identity_failed, _failure_details_truncated
    _sessions.clear()
    _process_diagnostics.clear()
    _counters.clear()
    _failures.clear()
    _failure_details_truncated = False
    _env_config = None
    _env_loaded = False
    _identity = None
    _identity_failed = False


def note_enclosing_transaction_opened(connection: Any) -> None:
    if not capture_enabled() or connection is None:
        return
    try:
        session = _ensure_session(connection)
        session.enclosing_open = True
        _instrument_connection(connection, session)
    except Exception as exc:
        _remember_failure("enclosing_note_failed", exc)


def note_enclosing_transaction_committed(connection: Any) -> None:
    """Optional explicit note for callers that commit before repository exit."""

    if not capture_enabled() or connection is None:
        return
    try:
        session = _sessions.get(id(connection))
        if session is None:
            return
        for item in session.pending:
            if not item.effects_discarded:
                item.effects_retained = True
        session.retaining_commit_observed = True
    except Exception as exc:
        _remember_failure("enclosing_commit_note_failed", exc)


def note_savepoint_opened(connection: Any, name: str) -> None:
    if not capture_enabled() or connection is None:
        return
    try:
        session = _ensure_session(connection)
        _instrument_connection(connection, session)
        session.stack.append(name)
    except Exception as exc:
        _remember_failure("savepoint_note_failed", exc)


def note_savepoint_released(connection: Any, name: str) -> None:
    _finish_savepoint(connection, name, SAVEPOINT_RELEASED)


def note_savepoint_rolled_back(connection: Any, name: str) -> None:
    _finish_savepoint(connection, name, SAVEPOINT_ROLLED_BACK)


def note_non_invocation(
    *,
    kind: str,
    detail: str,
    lifecycle_id: str | None = None,
    connection: Any = None,
) -> None:
    if not capture_enabled():
        return
    try:
        limits = active_bounds()
        diagnostic = {
            "diagnostic_id": "diag_" + secrets.token_hex(16),
            "recorded_at": datetime.now(UTC).isoformat(),
            "kind": kind,
            "lifecycle_id": lifecycle_id,
            "detail": detail[:500],
            "transaction_status": TX_COMMIT_UNKNOWN,
        }
        if connection is not None:
            session = _ensure_session(connection)
            _instrument_connection(connection, session)
            if len(session.diagnostics) >= limits.max_diagnostics:
                raise BoundExceeded("diagnostic_limit")
            session.diagnostics.append(diagnostic)
        else:
            if len(_process_diagnostics) >= limits.max_diagnostics:
                raise BoundExceeded("diagnostic_limit")
            _process_diagnostics.append(diagnostic)
        _counters[f"diagnostic_{kind}"] += 1
    except Exception as exc:
        _remember_failure("diagnostic_failed", exc)


def observe_repository_exit(
    connection: Any,
    *,
    committed: bool,
    commit_failed: bool,
    exit_rolled_back: bool = False,
) -> None:
    """Flush after the operational connection has already been closed.

    Callers must finish commit/rollback and close before invoking this. Flushing
    while the operational transaction is open is a protocol violation.
    """

    if connection is None:
        return
    session = _sessions.pop(id(connection), None)
    diagnostics = list(_process_diagnostics)
    _process_diagnostics.clear()
    if session is None and not diagnostics:
        return
    try:
        if session is not None:
            diagnostics.extend(session.diagnostics)
            _assign_disposition(
                session,
                committed=committed,
                commit_failed=commit_failed,
                exit_rolled_back=exit_rolled_back,
            )
            for diagnostic in diagnostics:
                diagnostic["transaction_status"] = _enclosing_status(
                    session,
                    committed=committed,
                    commit_failed=commit_failed,
                    exit_rolled_back=exit_rolled_back,
                )
            _flush(session.pending, diagnostics)
        elif diagnostics:
            _flush([], diagnostics)
    except Exception as exc:
        _remember_failure("flush_failed", exc)


def invoke_closed_candle_outcomes(
    evaluator: Callable[..., Any],
    *,
    caller_path: str,
    record: Any,
    execution_candles: Sequence[Any],
    execution_timeframe: str,
    decision_timestamp: Any,
    evaluated_at: str,
    repository: Any,
    scan_run_id: str | None = None,
    delivery: Any = None,
    handoff: Any = None,
    evidence_lineage: str = "unspecified",
) -> Any:
    enabled = False
    try:
        enabled = capture_enabled()
    except Exception as exc:
        _remember_failure("invoke_enabled_check_failed", exc)
        enabled = False
    if not enabled:
        return evaluator(
            record,
            execution_candles=execution_candles,
            execution_timeframe=execution_timeframe,
            decision_timestamp=decision_timestamp,
            evaluated_at=evaluated_at,
            repository=repository,
            scan_run_id=scan_run_id,
        )
    connection = getattr(repository, "connection", None)
    prepared_inputs: dict[str, Any] | None = None
    try:
        prepared_inputs = _collect_inputs(
            connection=connection,
            caller_path=caller_path,
            record=record,
            execution_candles=execution_candles,
            execution_timeframe=execution_timeframe,
            decision_timestamp=decision_timestamp,
            evaluated_at=evaluated_at,
            scan_run_id=scan_run_id,
            delivery=delivery,
            handoff=handoff,
            evidence_lineage=evidence_lineage,
        )
    except Exception as exc:
        _remember_failure("pre_capture_failed", exc)
        prepared_inputs = None
    try:
        result = evaluator(
            record,
            execution_candles=execution_candles,
            execution_timeframe=execution_timeframe,
            decision_timestamp=decision_timestamp,
            evaluated_at=evaluated_at,
            repository=repository,
            scan_run_id=scan_run_id,
        )
    except BaseException as exc:
        if prepared_inputs is not None and connection is not None:
            _buffer_outcome(connection, prepared_inputs, result=None, exception=exc)
        raise
    if prepared_inputs is not None and connection is not None:
        _buffer_outcome(connection, prepared_inputs, result=result, exception=None)
    return result


def _collect_inputs(**kwargs: Any) -> dict[str, Any]:
    limits = active_bounds()
    connection = kwargs["connection"]
    candles = kwargs["execution_candles"]
    if candles is None:
        raise BoundExceeded("candles_missing")
    if len(candles) > limits.max_candles:
        raise BoundExceeded("candle_limit")
    if connection is None:
        raise BoundExceeded("repository_connection_missing")
    identity = _remember_identity()
    session = _ensure_session(connection)
    _instrument_connection(connection, session)
    if len(session.pending) >= limits.max_pending_captures:
        raise BoundExceeded("pending_capture_limit")
    lifecycle_id = str(kwargs["record"].lifecycle_id)
    prestate = snapshot_dependency_closure(
        connection,
        lifecycle_id=lifecycle_id,
        scan_run_id=kwargs["scan_run_id"],
        limits=limits,
    )
    from app.research.durable_source_replay.delivery import _candle

    call = {
        "caller_path": kwargs["caller_path"],
        "evidence_lineage": kwargs["evidence_lineage"],
        "record": _model_dict(kwargs["record"]),
        "execution_candles": [_candle(item) for item in candles],
        "execution_timeframe": kwargs["execution_timeframe"],
        "decision_timestamp": kwargs["decision_timestamp"],
        "evaluated_at": kwargs["evaluated_at"],
        "scan_run_id": kwargs["scan_run_id"],
        "decision_timestamp_normalization": _describe_timestamp(kwargs["decision_timestamp"]),
    }
    delivery_payload, delivery_incomplete = _delivery_payload(
        kwargs["delivery"],
        kwargs["handoff"],
        limits,
    )
    policy = _policy_payload(kwargs["execution_timeframe"])
    return {
        "lifecycle_id": lifecycle_id,
        "caller_path": kwargs["caller_path"],
        "evidence_lineage": kwargs["evidence_lineage"],
        "call": call,
        "prestate": prestate,
        "delivery": delivery_payload,
        "delivery_incomplete": delivery_incomplete,
        "policy": policy,
        "parent_depth": int(delivery_payload.get("parent_depth") or 0),
        "identity": identity,
    }


def _buffer_outcome(
    connection: Any,
    prepared: Mapping[str, Any],
    *,
    result: Any,
    exception: BaseException | None,
) -> None:
    try:
        limits = active_bounds()
        effects = snapshot_effects(
            connection,
            lifecycle_id=str(prepared["lifecycle_id"]),
            limits=limits,
        )
        if exception is None:
            outcome: dict[str, Any] = {"exception": None, "evaluation": project_evaluation(result)}
            exception_class = None
            exception_message = None
        else:
            projected = project_exception(exception)
            outcome = {"exception": projected, "evaluation": None}
            exception_class = projected["exception_class"]
            exception_message = projected["exception_message"]
        incomplete_reason = prepared["delivery_incomplete"]
        payloads = {
            PAYLOAD_CALL: _encode_bounded(prepared["call"], limits),
            PAYLOAD_PRESTATE: _encode_bounded(prepared["prestate"], limits),
            PAYLOAD_DELIVERY: _encode_bounded(prepared["delivery"], limits),
            PAYLOAD_POLICY: _encode_bounded(prepared["policy"], limits),
            PAYLOAD_RESULT: _encode_bounded(outcome, limits),
            PAYLOAD_EFFECTS: _encode_bounded(effects, limits),
        }
        if len(payloads) > limits.max_reference_count:
            raise BoundExceeded("reference_count_limit")
        status = CAPTURE_INCOMPLETE if incomplete_reason else CAPTURE_COMPLETE
        identity = prepared["identity"]
        session = _ensure_session(connection)
        savepoint_name = session.stack[-1] if session.stack else None
        session.pending.append(
            _Pending(
                capture_id="cap_" + secrets.token_hex(16),
                savepoint_name=savepoint_name,
                savepoint_disposition=SAVEPOINT_OPEN if savepoint_name else SAVEPOINT_NONE,
                payloads=payloads,
                fields={
                    "caller_path": prepared["caller_path"],
                    "evidence_lineage": prepared["evidence_lineage"],
                    "application_schema_version": SCHEMA_VERSION,
                    "build_git_sha": identity["build"]["git_sha"],
                    "build_dirty": identity["build"]["dirty"],
                    "implementation_fingerprint": identity["fingerprint"],
                    "dependency_versions_json": json.dumps(identity["dependencies"], sort_keys=True),
                    "policy_id": prepared["policy"]["policy_id"],
                    "policy_family": prepared["policy"]["family"],
                    "policy_supported": bool(prepared["policy"]["supported"]),
                    "capture_status": status,
                    "incomplete_reason": incomplete_reason,
                    "savepoint_name": savepoint_name,
                    "exception_class": exception_class,
                    "exception_message": exception_message,
                    "parent_depth": prepared["parent_depth"],
                },
            )
        )
        _counters["buffered"] += 1
    except Exception as exc:
        _remember_failure("buffer_failed", exc)


def _flush(pending: Sequence[_Pending], diagnostics: Sequence[Mapping[str, Any]]) -> None:
    config = active_config()
    if config is None or not config.enabled or config.evidence_path is None:
        _counters["flush_without_path"] += 1
        return
    captures = []
    for item in pending:
        body = dict(item.fields)
        body["capture_id"] = item.capture_id
        body["payloads"] = item.payloads
        body["savepoint_disposition"] = item.savepoint_disposition
        body["enclosing_disposition"] = body.get("enclosing_disposition", TX_COMMIT_UNKNOWN)
        body["transaction_status"] = body.get("transaction_status", TX_COMMIT_UNKNOWN)
        captures.append(body)
    if not captures and not diagnostics:
        return
    write_bundle(
        config.evidence_path,
        captures=captures,
        diagnostics=diagnostics,
        operational_paths=config.operational_paths,
        limits=active_bounds(),
    )
    _counters["flushed_captures"] += len(captures)
    _counters["flushed_diagnostics"] += len(diagnostics)


def _assign_disposition(
    session: _Session,
    *,
    committed: bool,
    commit_failed: bool,
    exit_rolled_back: bool,
) -> None:
    enclosing = _enclosing_status(
        session,
        committed=committed,
        commit_failed=commit_failed,
        exit_rolled_back=exit_rolled_back,
    )
    for item in session.pending:
        if item.savepoint_disposition == SAVEPOINT_ROLLED_BACK:
            status = TX_SAVEPOINT_ROLLED_BACK
        elif item.effects_discarded:
            status = TX_ENCLOSING_ROLLED_BACK if session.enclosing_open else TX_COMMIT_UNKNOWN
        elif session.enclosing_open and (
            item.effects_retained or (committed and not item.effects_discarded and not session.rollback_observed)
        ):
            status = TX_ENCLOSING_COMMITTED
        elif not session.enclosing_open:
            # A commit happened, but no enclosing BEGIN was observed for this call.
            status = TX_COMMIT_UNKNOWN
        else:
            status = enclosing
        item.fields["transaction_status"] = status
        item.fields["enclosing_disposition"] = enclosing
        item.fields["savepoint_disposition"] = item.savepoint_disposition


def _enclosing_status(
    session: _Session,
    *,
    committed: bool,
    commit_failed: bool,
    exit_rolled_back: bool,
) -> str:
    if commit_failed:
        return TX_COMMIT_INTERRUPTED
    if not session.enclosing_open:
        return TX_COMMIT_UNKNOWN
    if session.rollback_observed and not any(item.effects_retained for item in session.pending):
        # A later empty commit after rollback does not retain discarded effects.
        return TX_ENCLOSING_ROLLED_BACK
    if any(item.effects_retained for item in session.pending):
        return TX_ENCLOSING_COMMITTED
    if committed and not session.rollback_observed:
        return TX_ENCLOSING_COMMITTED
    if exit_rolled_back or session.rollback_observed:
        return TX_ENCLOSING_ROLLED_BACK
    return TX_COMMIT_UNKNOWN


def _finish_savepoint(connection: Any, name: str, disposition: str) -> None:
    if connection is None:
        return
    session = _sessions.get(id(connection))
    if session is None:
        return
    try:
        if disposition == SAVEPOINT_ROLLED_BACK:
            while session.stack and session.stack[-1] != name:
                inner = session.stack.pop()
                _mark_savepoint(session, inner, SAVEPOINT_ROLLED_BACK)
        if session.stack and session.stack[-1] == name:
            session.stack.pop()
        _mark_savepoint(session, name, disposition)
    except Exception as exc:
        _remember_failure("savepoint_note_failed", exc)


def _mark_savepoint(session: _Session, name: str, disposition: str) -> None:
    for item in session.pending:
        if item.savepoint_name == name and item.savepoint_disposition == SAVEPOINT_OPEN:
            item.savepoint_disposition = disposition
            if disposition == SAVEPOINT_ROLLED_BACK:
                item.effects_discarded = True


def _ensure_session(connection: Any) -> _Session:
    key = id(connection)
    session = _sessions.get(key)
    if session is None:
        session = _Session()
        _sessions[key] = session
    return session


def _instrument_connection(connection: Any, session: _Session) -> None:
    if session.instrumented or connection is None:
        return
    if getattr(connection, "_dsr_instrumented", False):
        session.instrumented = True
        return
    original_commit = connection.commit
    original_rollback = connection.rollback

    def commit_wrapper(*args: Any, **kwargs: Any) -> Any:
        was_in_transaction = bool(getattr(connection, "in_transaction", False))
        result = original_commit(*args, **kwargs)
        live = _sessions.get(id(connection))
        if live is not None and was_in_transaction:
            for item in live.pending:
                if not item.effects_discarded:
                    item.effects_retained = True
            live.retaining_commit_observed = True
        return result

    def rollback_wrapper(*args: Any, **kwargs: Any) -> Any:
        live = _sessions.get(id(connection))
        if live is not None:
            live.rollback_observed = True
            for item in live.pending:
                if not item.effects_retained:
                    item.effects_discarded = True
        return original_rollback(*args, **kwargs)

    connection.commit = commit_wrapper  # type: ignore[method-assign]
    connection.rollback = rollback_wrapper  # type: ignore[method-assign]
    connection._dsr_instrumented = True  # type: ignore[attr-defined]
    session.instrumented = True


def _delivery_payload(delivery: Any, handoff: Any, limits: BoundLimits) -> tuple[dict[str, Any], str | None]:
    try:
        payload = serialize_delivery(delivery, handoff, limits=limits)
    except BoundExceeded as exc:
        return (
            {
                "delivery_present": delivery is not None,
                "handoff_present": handoff is not None,
                "truncated": True,
                "truncation_reason": exc.reason,
                "parent_depth": limits.max_parent_depth,
            },
            exc.reason,
        )
    except Exception as exc:
        return (
            {
                "delivery_present": delivery is not None,
                "handoff_present": handoff is not None,
                "truncated": True,
                "truncation_reason": f"delivery_encode_failed:{type(exc).__name__}",
                "parent_depth": 0,
            },
            f"delivery_encode_failed:{type(exc).__name__}",
        )
    return payload, None


def _policy_payload(execution_timeframe: str) -> dict[str, Any]:
    from app.research.evaluation_policy import build_runtime_evaluation_policy

    try:
        manifest = build_runtime_evaluation_policy(execution_timeframe=str(execution_timeframe))
    except Exception as exc:
        return {
            "supported": False,
            "reason": f"{type(exc).__name__}:{exc}",
            "family": "runtime_lifecycle_closed_candle",
            "policy_id": "unsupported",
            "canonical_text": None,
            "execution_timeframe_argument": execution_timeframe,
        }
    return {
        "supported": True,
        "reason": None,
        "family": manifest.family,
        "policy_id": manifest.policy_id,
        "canonical_text": manifest.canonical_bytes.decode("utf-8"),
        "execution_timeframe_argument": execution_timeframe,
    }


def _encode_bounded(value: Any, limits: BoundLimits) -> bytes:
    try:
        payload = encode_canonical(value)
    except CodecError as exc:
        raise BoundExceeded(f"codec:{exc.reason}") from exc
    if len(payload) > limits.max_payload_bytes:
        raise BoundExceeded("payload_byte_limit")
    return payload


def _describe_timestamp(value: Any) -> dict[str, str]:
    try:
        normalized = normalize_utc_timestamp(value, field_name="decision_timestamp")
    except Exception as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
    return {"status": "normalized", "utc": normalized.isoformat()}


def _remember_identity() -> dict[str, Any]:
    global _identity, _identity_failed
    if _identity is not None:
        return _identity
    if _identity_failed:
        raise RuntimeError("identity_previously_failed")
    try:
        _identity = {
            "build": build_identity(),
            "fingerprint": implementation_fingerprint(),
            "dependencies": dependency_versions(),
        }
    except Exception:
        _identity_failed = True
        raise
    return _identity


def _remember_failure(kind: str, exc: BaseException) -> None:
    global _failure_details_truncated
    _counters[kind] += 1
    _counters["capture_failures_total"] += 1
    limits = active_bounds()
    if len(_failures) >= limits.max_failure_details:
        _failure_details_truncated = True
        _counters["failure_details_dropped"] += 1
        return
    _failures.append({"kind": kind, "reason": f"{type(exc).__name__}:{exc}"[:500]})


def _load_env_once() -> None:
    global _env_config, _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        enabled = os.environ.get("SOURCE_REPLAY_CAPTURE_ENABLED", "")
        path_text = os.environ.get("SOURCE_REPLAY_EVIDENCE_PATH", "")
        if enabled != "true":
            _env_config = CaptureConfig(enabled=False, evidence_path=None)
            return
        if not path_text.strip():
            _env_config = CaptureConfig(
                enabled=False,
                evidence_path=None,
                disabled_reason="evidence_path_required",
            )
            _counters["capture_misconfigured"] += 1
            _remember_failure("capture_misconfigured", ValueError("evidence_path_required"))
            return
        from app.research.durable_source_replay.paths import EvidencePathError, resolve_evidence_path

        try:
            resolved = resolve_evidence_path(path_text)
        except EvidencePathError as exc:
            _env_config = CaptureConfig(enabled=False, evidence_path=None, disabled_reason=exc.reason)
            _counters["capture_misconfigured"] += 1
            _remember_failure("capture_misconfigured", exc)
            return
        try:
            _remember_identity()
        except Exception as exc:
            _env_config = CaptureConfig(
                enabled=False,
                evidence_path=None,
                disabled_reason="identity_init_failed",
            )
            _remember_failure("identity_init_failed", exc)
            return
        _env_config = CaptureConfig(enabled=True, evidence_path=resolved)
    except Exception as exc:
        _env_config = CaptureConfig(enabled=False, evidence_path=None, disabled_reason="env_load_failed")
        _remember_failure("env_load_failed", exc)


__all__ = [
    "CALLER_LIFECYCLE_SERVICE",
    "CALLER_OWNER_MONITORING",
    "CaptureConfig",
    "active_config",
    "capture_counters",
    "capture_enabled",
    "capture_failures",
    "invoke_closed_candle_outcomes",
    "note_enclosing_transaction_committed",
    "note_enclosing_transaction_opened",
    "note_non_invocation",
    "note_savepoint_opened",
    "note_savepoint_released",
    "note_savepoint_rolled_back",
    "observe_repository_exit",
    "replay_guard",
    "reset_capture_process_state",
    "use_capture",
]
