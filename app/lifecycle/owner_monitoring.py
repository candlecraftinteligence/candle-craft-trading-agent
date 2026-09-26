"""Monitor unresolved economic plans independently of discovery selection.

Discovery answers whether a new setup should be admitted. This module answers
what happened to plans CCI already owns. It does not create setups, change
gates, or force a symbol back into the new-opportunity universe.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.data.candle_integrity import closed_candles_as_of, normalize_utc_timestamp
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleRecord
from app.lifecycle.outcome_policy import compatible_plan_identities
from app.lifecycle.outcomes import (
    OUTCOME_ELIGIBLE_STATES,
    TERMINAL_OUTCOME_STATES,
    evaluate_closed_candle_outcomes,
    record_closed_candle_evidence_gap,
)
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.runtime_epoch.models import RuntimeEpochIdentity
from app.universe.symbol_universe import UniverseResolutionError

GAP_REQUIRED_CLOSED_CANDLE = "required_closed_candle_unavailable"
GAP_EXCHANGE_MARKET_DATA = "exchange_market_data_unavailable"
GAP_MARKET_UNSUPPORTED = "market_unsupported_or_delisted"
GAP_CURSOR_INTERVAL = "irrecoverable_cursor_interval"

CandleFetcher = Callable[[str, str, int], Awaitable[Sequence[Any]]]


@dataclass(frozen=True)
class SymbolMonitoringEvidence:
    candles: tuple[Any, ...] = ()
    execution_timeframe: str = "15m"
    decision_timestamp: str = ""
    gap_reason: str | None = None


@dataclass(frozen=True)
class TrackingCursorLag:
    lifecycle_id: str
    symbol: str
    mode: str
    direction: str
    plan_version_id: str | None
    current_state: str
    is_current: bool
    last_processed_close_at: str | None
    latest_available_close_at: str | None
    lag_seconds: int | None
    gap_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id,
            "symbol": self.symbol,
            "mode": self.mode,
            "direction": self.direction,
            "plan_version_id": self.plan_version_id,
            "current_state": self.current_state,
            "is_current": self.is_current,
            "last_processed_close_at": self.last_processed_close_at,
            "latest_available_close_at": self.latest_available_close_at,
            "lag_seconds": self.lag_seconds,
            "gap_reason": self.gap_reason,
        }


@dataclass(frozen=True)
class OwnerMonitoringResult:
    evaluated_lifecycle_ids: tuple[str, ...]
    uncovered_symbols: tuple[str, ...]
    gap_lifecycle_ids: tuple[str, ...]
    errors: tuple[dict[str, str], ...]
    lags: tuple[TrackingCursorLag, ...]
    covered_keys: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluated_lifecycle_ids": list(self.evaluated_lifecycle_ids),
            "uncovered_symbols": list(self.uncovered_symbols),
            "gap_lifecycle_ids": list(self.gap_lifecycle_ids),
            "errors": [dict(item) for item in self.errors],
            "lags": [item.as_dict() for item in self.lags],
            "covered_keys": list(self.covered_keys),
            "obligation_count": len(self.lags),
        }


def select_tracking_obligations(
    repository: SQLiteSetupLifecycleRepository,
) -> tuple[SetupLifecycleRecord, ...]:
    """Return unresolved economic owners that still require outcome tracking.

    Eligibility is the outcome policy's eligible states, restricted to a latched
    plan or an existing non-terminal progress row. ``is_current`` is not a
    stop condition. Terminal states and terminal progress are excluded.
    """

    candidates = repository.list_economic_tracking_candidates(tuple(OUTCOME_ELIGIBLE_STATES))
    if not candidates:
        return ()
    progress_rows = repository.list_outcome_progress_for_lifecycles(
        tuple(record.lifecycle_id for record in candidates)
    )
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in progress_rows:
        grouped[row.lifecycle_id].append(row)
    return tuple(
        record
        for record in candidates
        if _obligation_is_open(record, tuple(grouped.get(record.lifecycle_id, ())))
    )


def diagnose_tracking_cursor_lag(
    record: SetupLifecycleRecord,
    *,
    last_processed_close_at: str | None,
    latest_available_close_at: str | None,
    gap_reason: str | None = None,
) -> TrackingCursorLag:
    """Observational cursor lag. This does not change eligibility or strategy."""

    return TrackingCursorLag(
        lifecycle_id=record.lifecycle_id,
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        plan_version_id=record.plan_version_id,
        current_state=record.current_state.value,
        is_current=bool(record.is_current),
        last_processed_close_at=last_processed_close_at,
        latest_available_close_at=latest_available_close_at,
        lag_seconds=_lag_seconds(last_processed_close_at, latest_available_close_at),
        gap_reason=gap_reason,
    )


def evidence_key(symbol: str, execution_timeframe: str) -> str:
    return f"{symbol.strip().upper()}|{execution_timeframe.strip().lower()}"


def evidence_from_symbol_results(
    symbol_results: Sequence[Any],
    *,
    decision_fallback: str,
) -> dict[str, SymbolMonitoringEvidence]:
    evidence: dict[str, SymbolMonitoringEvidence] = {}
    for symbol_result in symbol_results:
        candles = getattr(symbol_result, "lifecycle_execution_candles", None)
        if candles is None:
            continue
        timeframe = _text(getattr(symbol_result, "lifecycle_execution_timeframe", None))
        if timeframe == NA:
            timeframe = "15m"
        decision = getattr(symbol_result, "lifecycle_decision_timestamp", None) or decision_fallback
        decision_text = decision.isoformat() if hasattr(decision, "isoformat") else str(decision)
        symbol = _text(getattr(symbol_result, "symbol", "")).upper()
        if symbol == NA:
            continue
        evidence[evidence_key(symbol, timeframe)] = SymbolMonitoringEvidence(
            candles=tuple(candles),
            execution_timeframe=timeframe.lower(),
            decision_timestamp=decision_text,
        )
    return evidence


def monitor_tracking_obligations(
    repository: SQLiteSetupLifecycleRepository,
    *,
    evidence_by_key: Mapping[str, SymbolMonitoringEvidence],
    evaluated_at: str,
    scan_run_id: str | None,
    default_timeframe: str = "15m",
    record_missing_evidence: bool = False,
    skip_lifecycle_ids: Collection[str] = (),
) -> OwnerMonitoringResult:
    """Evaluate every open economic obligation that has candle evidence.

    Owners without evidence are reported as uncovered. When
    ``record_missing_evidence`` is set, that absence is persisted as an
    explicit gap and does not become a terminal outcome.
    """

    obligations = select_tracking_obligations(repository)
    progress_rows = repository.list_outcome_progress_for_lifecycles(
        tuple(record.lifecycle_id for record in obligations)
    )
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in progress_rows:
        grouped[row.lifecycle_id].append(row)

    skip = set(skip_lifecycle_ids)
    evaluated: list[str] = []
    uncovered: list[str] = []
    gaps: list[str] = []
    errors: list[dict[str, str]] = []
    lags: list[TrackingCursorLag] = []
    covered: list[str] = []
    connection = repository.connection
    assert connection is not None

    for record in obligations:
        if record.lifecycle_id in skip:
            continue
        rows = tuple(grouped.get(record.lifecycle_id, ()))
        timeframe = _obligation_timeframe(rows, default_timeframe)
        key = evidence_key(record.symbol, timeframe)
        evidence = evidence_by_key.get(key)
        if evidence is None or (not evidence.candles and evidence.gap_reason is None):
            if record_missing_evidence:
                gap_reason = GAP_REQUIRED_CLOSED_CANDLE if evidence is None else GAP_REQUIRED_CLOSED_CANDLE
                _persist_gap(
                    repository,
                    record,
                    diagnostic=gap_reason,
                    execution_timeframe=timeframe,
                    evaluated_at=evaluated_at,
                    scan_run_id=scan_run_id,
                    errors=errors,
                )
                gaps.append(record.lifecycle_id)
                lags.append(
                    diagnose_tracking_cursor_lag(
                        record,
                        last_processed_close_at=_last_processed_close(rows),
                        latest_available_close_at=None,
                        gap_reason=gap_reason,
                    )
                )
            else:
                uncovered.append(record.symbol)
                lags.append(
                    diagnose_tracking_cursor_lag(
                        record,
                        last_processed_close_at=_last_processed_close(rows),
                        latest_available_close_at=None,
                        gap_reason=None,
                    )
                )
            continue

        if evidence.gap_reason:
            diagnostic = evidence.gap_reason
            if diagnostic.startswith("expected open") or "continuity" in diagnostic:
                diagnostic = f"{GAP_CURSOR_INTERVAL}:{diagnostic}"
            _persist_gap(
                repository,
                record,
                diagnostic=diagnostic,
                execution_timeframe=timeframe,
                evaluated_at=evaluated_at,
                scan_run_id=scan_run_id,
                errors=errors,
            )
            gaps.append(record.lifecycle_id)
            lags.append(
                diagnose_tracking_cursor_lag(
                    record,
                    last_processed_close_at=_last_processed_close(rows),
                    latest_available_close_at=None,
                    gap_reason=diagnostic,
                )
            )
            continue

        connection.execute("SAVEPOINT owner_monitor")
        try:
            evaluation = evaluate_closed_candle_outcomes(
                record,
                execution_candles=evidence.candles,
                execution_timeframe=evidence.execution_timeframe or timeframe,
                decision_timestamp=evidence.decision_timestamp or evaluated_at,
                evaluated_at=evaluated_at,
                repository=repository,
                scan_run_id=scan_run_id,
            )
        except Exception as exc:
            connection.execute("ROLLBACK TO owner_monitor")
            connection.execute("RELEASE owner_monitor")
            errors.append(
                {
                    "lifecycle_id": record.lifecycle_id,
                    "symbol": record.symbol,
                    "detail": f"{type(exc).__name__}:{exc}",
                }
            )
            continue
        else:
            connection.execute("RELEASE owner_monitor")

        evaluated.append(record.lifecycle_id)
        covered.append(key)
        progress = evaluation.progress
        gap_reason = _gap_reason_from_progress(progress)
        if gap_reason is not None:
            gaps.append(record.lifecycle_id)
        latest_close = _latest_close(
            evidence.candles,
            evidence.execution_timeframe or timeframe,
            evidence.decision_timestamp or evaluated_at,
        )
        last_close = progress.evaluation_cursor_close_at if progress is not None else _last_processed_close(rows)
        lags.append(
            diagnose_tracking_cursor_lag(
                evaluation.record,
                last_processed_close_at=last_close,
                latest_available_close_at=latest_close,
                gap_reason=gap_reason,
            )
        )

    return OwnerMonitoringResult(
        evaluated_lifecycle_ids=tuple(evaluated),
        uncovered_symbols=tuple(dict.fromkeys(uncovered)),
        gap_lifecycle_ids=tuple(dict.fromkeys(gaps)),
        errors=tuple(errors),
        lags=tuple(lags),
        covered_keys=tuple(dict.fromkeys(covered)),
    )


async def monitor_obligations_with_market_data(
    database_path: Path | str,
    *,
    evidence_by_key: Mapping[str, SymbolMonitoringEvidence],
    fetch_candles: CandleFetcher | None,
    execution_timeframe: str,
    candle_limit: int,
    evaluated_at: str,
    scan_run_id: str | None,
    expected_identity: RuntimeEpochIdentity | None = None,
    skip_keys: Collection[str] = (),
    skip_lifecycle_ids: Collection[str] = (),
    record_missing_evidence: bool = True,
) -> OwnerMonitoringResult:
    """Fetch exchange candles for obligations discovery did not already cover.

    Network reads happen before the write transaction. A fetch failure becomes
    an explicit gap. It does not synthesize candles or a terminal outcome.
    """

    with SQLiteSetupLifecycleRepository(
        database_path,
        expected_identity=expected_identity,
    ) as repository:
        obligations = select_tracking_obligations(repository)
        progress_rows = repository.list_outcome_progress_for_lifecycles(
            tuple(record.lifecycle_id for record in obligations)
        )
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in progress_rows:
        grouped[row.lifecycle_id].append(row)

    skipped = set(skip_keys)
    needed: dict[str, tuple[str, str]] = {}
    for record in obligations:
        rows = tuple(grouped.get(record.lifecycle_id, ()))
        timeframe = _obligation_timeframe(rows, execution_timeframe)
        key = evidence_key(record.symbol, timeframe)
        if key in skipped or _evidence_covers(evidence_by_key.get(key)):
            continue
        needed[key] = (record.symbol, timeframe)

    merged = dict(evidence_by_key)
    for key, (symbol, timeframe) in needed.items():
        if fetch_candles is None:
            merged[key] = SymbolMonitoringEvidence(
                candles=(),
                execution_timeframe=timeframe,
                decision_timestamp=evaluated_at,
                gap_reason=GAP_REQUIRED_CLOSED_CANDLE,
            )
            continue
        try:
            candles = await fetch_candles(symbol, timeframe, candle_limit)
        except Exception as exc:
            merged[key] = SymbolMonitoringEvidence(
                candles=(),
                execution_timeframe=timeframe,
                decision_timestamp=evaluated_at,
                gap_reason=classify_market_data_failure(exc),
            )
            continue
        candle_tuple = tuple(candles or ())
        if not candle_tuple:
            merged[key] = SymbolMonitoringEvidence(
                candles=(),
                execution_timeframe=timeframe,
                decision_timestamp=evaluated_at,
                gap_reason=GAP_REQUIRED_CLOSED_CANDLE,
            )
            continue
        merged[key] = SymbolMonitoringEvidence(
            candles=candle_tuple,
            execution_timeframe=timeframe,
            decision_timestamp=evaluated_at,
        )

    with SQLiteSetupLifecycleRepository(
        database_path,
        expected_identity=expected_identity,
    ) as repository:
        connection = repository.connection
        assert connection is not None
        started = not connection.in_transaction
        if started:
            connection.execute("BEGIN IMMEDIATE")
        try:
            result = monitor_tracking_obligations(
                repository,
                evidence_by_key=merged,
                evaluated_at=evaluated_at,
                scan_run_id=scan_run_id,
                default_timeframe=execution_timeframe,
                record_missing_evidence=record_missing_evidence,
                skip_lifecycle_ids=skip_lifecycle_ids,
            )
        except Exception:
            if started and connection.in_transaction:
                connection.rollback()
            raise
        else:
            if started and connection.in_transaction:
                connection.commit()
        return result


def discovery_failure_continues_owner_monitoring(exc: BaseException) -> bool:
    """Ranking/universe acquisition failure must not block owned-plan monitoring."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, UniverseResolutionError):
            return True
        cause = current.__cause__
        context = current.__context__
        current = cause if isinstance(cause, BaseException) else None
        if current is None and isinstance(context, BaseException) and context is not exc:
            current = context
    return False


def classify_market_data_failure(exc: BaseException) -> str:
    text = str(exc).lower()
    if any(token in text for token in ("invalid symbol", "unknown symbol", "delist", "not listed")):
        return GAP_MARKET_UNSUPPORTED
    return f"{GAP_EXCHANGE_MARKET_DATA}:{type(exc).__name__}"


def _evidence_covers(evidence: SymbolMonitoringEvidence | None) -> bool:
    """Candle payload or an already classified gap. An empty payload is not coverage."""

    if evidence is None:
        return False
    return bool(evidence.candles) or evidence.gap_reason is not None


def _obligation_is_open(record: SetupLifecycleRecord, progress_rows: tuple[Any, ...]) -> bool:
    if record.current_state in TERMINAL_OUTCOME_STATES:
        return False
    if record.current_state not in OUTCOME_ELIGIBLE_STATES:
        return False
    identities = set(compatible_plan_identities(record))
    relevant = tuple(row for row in progress_rows if row.plan_identity in identities)
    if relevant and all(_progress_is_terminal(row) for row in relevant):
        return False
    if record.plan_version_id:
        return True
    return any(not _progress_is_terminal(row) for row in progress_rows)


def _progress_is_terminal(progress: Any) -> bool:
    text = str(getattr(progress, "terminal_outcome", "") or "").strip()
    return bool(text) and text.upper() != NA


def _obligation_timeframe(progress_rows: Sequence[Any], default_timeframe: str) -> str:
    for row in progress_rows:
        timeframe = _text(getattr(row, "execution_timeframe", None)).lower()
        if timeframe != NA.lower():
            return timeframe
    fallback = _text(default_timeframe).lower()
    return "15m" if fallback == NA.lower() else fallback


def _last_processed_close(progress_rows: Sequence[Any]) -> str | None:
    closes = [
        row.evaluation_cursor_close_at
        for row in progress_rows
        if getattr(row, "evaluation_cursor_close_at", None)
    ]
    if not closes:
        return None
    return max(closes)


def _latest_close(candles: Sequence[Any], timeframe: str, decision_timestamp: str) -> str | None:
    if not candles:
        return None
    try:
        window = closed_candles_as_of(
            candles,
            timeframe=timeframe,
            decision_timestamp=decision_timestamp,
            minimum_closed_history=0,
            require_continuity=False,
        )
    except (TypeError, ValueError):
        return None
    if not window.timeline:
        return None
    return window.timeline[-1].close_timestamp.isoformat()


def _lag_seconds(last_processed_close_at: str | None, latest_available_close_at: str | None) -> int | None:
    if not last_processed_close_at or not latest_available_close_at:
        return None
    try:
        last_close = normalize_utc_timestamp(last_processed_close_at, field_name="last_processed_close_at")
        latest_close = normalize_utc_timestamp(
            latest_available_close_at,
            field_name="latest_available_close_at",
        )
    except ValueError:
        return None
    if not isinstance(last_close, datetime) or not isinstance(latest_close, datetime):
        return None
    return int((latest_close - last_close).total_seconds())


def _gap_reason_from_progress(progress: Any) -> str | None:
    if progress is None:
        return None
    status = _text(getattr(progress, "integrity_status", None))
    diagnostic = _text(getattr(progress, "diagnostic", None))
    if status.lower() == "verified" or diagnostic == NA:
        return None
    if diagnostic == NA:
        return None
    return diagnostic


def _persist_gap(
    repository: SQLiteSetupLifecycleRepository,
    record: SetupLifecycleRecord,
    *,
    diagnostic: str,
    execution_timeframe: str,
    evaluated_at: str,
    scan_run_id: str | None,
    errors: list[dict[str, str]],
) -> None:
    connection = repository.connection
    assert connection is not None
    connection.execute("SAVEPOINT owner_monitor_gap")
    try:
        record_closed_candle_evidence_gap(
            record,
            diagnostic=diagnostic,
            execution_timeframe=execution_timeframe,
            evaluated_at=evaluated_at,
            repository=repository,
            scan_run_id=scan_run_id,
        )
    except Exception as exc:
        connection.execute("ROLLBACK TO owner_monitor_gap")
        connection.execute("RELEASE owner_monitor_gap")
        errors.append(
            {
                "lifecycle_id": record.lifecycle_id,
                "symbol": record.symbol,
                "detail": f"{type(exc).__name__}:{exc}",
            }
        )
        return
    connection.execute("RELEASE owner_monitor_gap")


def _text(value: Any) -> str:
    if value is None:
        return NA
    text = str(value).strip()
    return text if text else NA


__all__ = [
    "GAP_CURSOR_INTERVAL",
    "GAP_EXCHANGE_MARKET_DATA",
    "GAP_MARKET_UNSUPPORTED",
    "GAP_REQUIRED_CLOSED_CANDLE",
    "OwnerMonitoringResult",
    "SymbolMonitoringEvidence",
    "TrackingCursorLag",
    "classify_market_data_failure",
    "diagnose_tracking_cursor_lag",
    "discovery_failure_continues_owner_monitoring",
    "evidence_from_symbol_results",
    "evidence_key",
    "monitor_obligations_with_market_data",
    "monitor_tracking_obligations",
    "select_tracking_obligations",
]
