"""Operational run registration and per-symbol origin eligibility."""

from __future__ import annotations

import sqlite3
from typing import Any
from uuid import uuid4

from app.data.dtos import NA
from app.runtime_epoch.authority import require_active_runtime_epoch
from app.runtime_epoch.errors import RuntimeEpochConfigurationError, RuntimeEpochOriginError
from app.runtime_epoch.models import (
    LIVE_SCAN_EVALUATION,
    ORIGIN_KIND_LIVE_FRESH,
    ORIGIN_STATUS_BLOCKED,
    ORIGIN_STATUS_GRANTED,
    RUN_STATUS_REGISTERED,
    OperationalRunRegistration,
    RuntimeEpochRecord,
    SymbolOriginDecision,
)
from app.runtime_epoch.time_contract import comparable_utc, parse_utc, strictly_after

_LIVE_ORIGIN_KINDS = frozenset({LIVE_SCAN_EVALUATION})


def register_operational_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    registered_at: str,
    producer_started_at: str | None = None,
    epoch: RuntimeEpochRecord | None = None,
) -> OperationalRunRegistration:
    """Persist live-run registration before lifecycle or public effects."""

    epoch = epoch or require_active_runtime_epoch(connection)
    normalized_run_id = _required_text(run_id, "run_id")
    registered = _required_utc(registered_at, "registered_at")
    if not strictly_after(registered, epoch.cutoff_at):
        raise RuntimeEpochOriginError(
            "Operational run registration must be strictly after the epoch UTC cutoff."
        )
    producer = comparable_utc(producer_started_at)
    if producer is not None and parse_utc(producer) is not None and parse_utc(registered) is not None:
        if parse_utc(producer) > parse_utc(registered):  # type: ignore[operator]
            raise RuntimeEpochOriginError("Producer start cannot be after run registration.")
    existing = connection.execute(
        "SELECT * FROM runtime_operational_runs WHERE run_id = ?",
        (normalized_run_id,),
    ).fetchone()
    if existing is not None:
        if str(existing["runtime_epoch_id"]) != epoch.epoch_id:
            raise RuntimeEpochOriginError("Run registration is bound to a different runtime epoch.")
        return OperationalRunRegistration(
            run_id=str(existing["run_id"]),
            runtime_epoch_id=str(existing["runtime_epoch_id"]),
            registered_at=str(existing["registered_at"]),
            status=str(existing["status"]),
            producer_started_at=existing["producer_started_at"],
        )
    connection.execute(
        """
        INSERT INTO runtime_operational_runs (
            run_id, runtime_epoch_id, registered_at, status, producer_started_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (normalized_run_id, epoch.epoch_id, registered, RUN_STATUS_REGISTERED, producer),
    )
    return OperationalRunRegistration(
        run_id=normalized_run_id,
        runtime_epoch_id=epoch.epoch_id,
        registered_at=registered,
        status=RUN_STATUS_REGISTERED,
        producer_started_at=producer,
    )


def evaluate_symbol_origin(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    symbol: str,
    evaluation_kind: str,
    evaluation_completed_at: str | None,
    decision_cutoff_at: str | None,
    producer_observed_at: str | None = None,
    epoch: RuntimeEpochRecord | None = None,
) -> SymbolOriginDecision:
    """Grant origin only for a post-boundary live evaluation of this symbol."""

    epoch = epoch or require_active_runtime_epoch(connection)
    normalized_run_id = _required_text(run_id, "run_id")
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return _blocked(normalized_run_id, "UNKNOWN", "missing_symbol")
    run_row = connection.execute(
        "SELECT * FROM runtime_operational_runs WHERE run_id = ?",
        (normalized_run_id,),
    ).fetchone()
    if run_row is None:
        return _blocked(normalized_run_id, normalized_symbol, "run_not_registered")
    if str(run_row["runtime_epoch_id"]) != epoch.epoch_id:
        return _blocked(normalized_run_id, normalized_symbol, "run_epoch_mismatch")

    kind = str(evaluation_kind or UNSPECIFIED).strip().lower() or UNSPECIFIED
    if kind not in _LIVE_ORIGIN_KINDS:
        return _blocked(normalized_run_id, normalized_symbol, f"evaluation_kind_not_live:{kind}")

    evaluation_at = comparable_utc(evaluation_completed_at)
    cutoff_at = comparable_utc(decision_cutoff_at)
    producer_at = comparable_utc(producer_observed_at) or evaluation_at
    if evaluation_at is None or cutoff_at is None or producer_at is None:
        return _blocked(normalized_run_id, normalized_symbol, "origin_times_unknown")
    if not strictly_after(evaluation_at, epoch.cutoff_at):
        return _blocked(normalized_run_id, normalized_symbol, "evaluation_not_after_epoch_cutoff")
    if not strictly_after(cutoff_at, epoch.cutoff_at):
        return _blocked(normalized_run_id, normalized_symbol, "decision_cutoff_not_after_epoch_cutoff")
    if not strictly_after(producer_at, epoch.cutoff_at):
        return _blocked(normalized_run_id, normalized_symbol, "producer_observation_not_after_epoch_cutoff")
    registered_at = str(run_row["registered_at"])
    if parse_utc(evaluation_at) is not None and parse_utc(registered_at) is not None:
        if parse_utc(evaluation_at) < parse_utc(registered_at):  # type: ignore[operator]
            return _blocked(normalized_run_id, normalized_symbol, "evaluation_before_run_registration")
    if parse_utc(cutoff_at) is not None and parse_utc(evaluation_at) is not None:
        if parse_utc(cutoff_at) > parse_utc(evaluation_at):  # type: ignore[operator]
            return _blocked(normalized_run_id, normalized_symbol, "decision_cutoff_after_evaluation")

    existing = connection.execute(
        "SELECT * FROM runtime_operational_origins WHERE run_id = ? AND symbol = ?",
        (normalized_run_id, normalized_symbol),
    ).fetchone()
    if existing is not None:
        if str(existing["status"]) == ORIGIN_STATUS_GRANTED:
            return SymbolOriginDecision(
                granted=True,
                origin_id=str(existing["origin_id"]),
                run_id=normalized_run_id,
                symbol=normalized_symbol,
                reason=NA,
                evaluation_completed_at=str(existing["evaluation_completed_at"]),
                decision_cutoff_at=str(existing["decision_cutoff_at"]),
                producer_observed_at=str(existing["producer_observed_at"]),
                origin_kind=str(existing["origin_kind"]),
            )
        return _blocked(normalized_run_id, normalized_symbol, str(existing["block_reason"] or "origin_previously_blocked"))

    origin_id = uuid4().hex
    connection.execute(
        """
        INSERT INTO runtime_operational_origins (
            origin_id, runtime_epoch_id, run_id, symbol, evaluation_completed_at,
            decision_cutoff_at, producer_observed_at, origin_kind, status, block_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            origin_id,
            epoch.epoch_id,
            normalized_run_id,
            normalized_symbol,
            evaluation_at,
            cutoff_at,
            producer_at,
            ORIGIN_KIND_LIVE_FRESH,
            ORIGIN_STATUS_GRANTED,
            None,
            evaluation_at,
        ),
    )
    return SymbolOriginDecision(
        granted=True,
        origin_id=origin_id,
        run_id=normalized_run_id,
        symbol=normalized_symbol,
        reason=NA,
        evaluation_completed_at=evaluation_at,
        decision_cutoff_at=cutoff_at,
        producer_observed_at=producer_at,
        origin_kind=ORIGIN_KIND_LIVE_FRESH,
    )


def persist_blocked_symbol_origin(
    connection: sqlite3.Connection,
    decision: SymbolOriginDecision,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> None:
    if decision.granted or decision.origin_id is not None:
        return
    epoch = epoch or require_active_runtime_epoch(connection)
    existing = connection.execute(
        "SELECT origin_id FROM runtime_operational_origins WHERE run_id = ? AND symbol = ?",
        (decision.run_id, decision.symbol),
    ).fetchone()
    if existing is not None:
        return
    connection.execute(
        """
        INSERT INTO runtime_operational_origins (
            origin_id, runtime_epoch_id, run_id, symbol, evaluation_completed_at,
            decision_cutoff_at, producer_observed_at, origin_kind, status, block_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            epoch.epoch_id,
            decision.run_id,
            decision.symbol,
            decision.evaluation_completed_at,
            decision.decision_cutoff_at,
            decision.producer_observed_at,
            decision.origin_kind,
            ORIGIN_STATUS_BLOCKED,
            decision.reason,
            decision.evaluation_completed_at,
        ),
    )


def load_granted_origin(
    connection: sqlite3.Connection,
    origin_id: str,
) -> sqlite3.Row | None:
    normalized = str(origin_id or "").strip()
    if not normalized:
        return None
    return connection.execute(
        "SELECT * FROM runtime_operational_origins WHERE origin_id = ? AND status = ?",
        (normalized, ORIGIN_STATUS_GRANTED),
    ).fetchone()


def _blocked(run_id: str, symbol: str, reason: str) -> SymbolOriginDecision:
    return SymbolOriginDecision(
        granted=False,
        origin_id=None,
        run_id=run_id,
        symbol=symbol,
        reason=reason,
    )


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or text.upper() == NA:
        raise RuntimeEpochOriginError(f"Operational origin {field_name} is required.")
    return text


def _required_utc(value: Any, field_name: str) -> str:
    parsed = comparable_utc(value)
    if parsed is None:
        raise RuntimeEpochOriginError(f"Operational origin {field_name} must be a parseable UTC timestamp.")
    return parsed


UNSPECIFIED = "unspecified"
