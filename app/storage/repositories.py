from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import astuple
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from app.analytics.portfolio_selection import PortfolioSelectionResult
from app.analytics.symbol_health import symbol_health_events_from_results, update_symbol_health_records
from app.backtesting import ReplaySummary
from app.data.dtos import NA
from app.formatters.scanner_display import RankedSymbolDisplay, display_fields
from app.pipeline.scanner_runner import ScannerRunResult, ScannerSymbolResult
from app.storage.database import (
    DEFAULT_DATABASE_PATH,
    DatabaseMissingError,
    StorageError,
    open_initialized_database,
    open_read_only_database,
)
from app.storage.models import (
    ReplayResultRecord,
    ScanHistorySummary,
    ScanRunRecord,
    SetupCandidateRecord,
    SymbolResultRecord,
    WatchIterationMetadata,
)
from app.storage.symbol_health import (
    _load_symbol_health_records,
    _insert_symbol_health_events,
    _upsert_symbol_health_records,
    symbol_health_records_from_payload,
)


def store_scan_result(
    database_path: Path | str,
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    replay_summary: ReplaySummary | None = None,
    portfolio_selection: PortfolioSelectionResult | None = None,
    command_preset: str | None = None,
    command_used: str | None = None,
    raw_payload: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    watch_iteration: WatchIterationMetadata | None = None,
) -> str:
    run_id = run_id or uuid4().hex
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ranked_by_symbol = {item.symbol_result.symbol: item for item in ranked_results or ()}
    payload = _json_ready(
        raw_payload
        if raw_payload is not None
        else _storage_payload(result, ranked_by_symbol, replay_summary, portfolio_selection)
    )
    portfolio_decisions = _portfolio_decisions(portfolio_selection)

    scan_record = _scan_run_record(
        run_id=run_id,
        timestamp=timestamp,
        result=result,
        command_preset=command_preset,
        command_used=command_used,
        raw_payload=payload,
        watch_iteration=watch_iteration,
    )
    symbol_records = tuple(
        _symbol_result_record(
            run_id=run_id,
            result=result,
            symbol_result=symbol_result,
            ranked=ranked_by_symbol.get(symbol_result.symbol),
            portfolio_decision=portfolio_decisions.get(symbol_result.symbol, NA),
        )
        for symbol_result in result.results
    )
    setup_records = tuple(
        record
        for record in (_setup_candidate_record(run_id, symbol_result) for symbol_result in result.results)
        if record is not None
    )
    replay_records = tuple(_replay_result_records(run_id, replay_summary, result.market_regime.state.value))

    try:
        with closing(open_initialized_database(database_path)) as connection:
            _insert_scan_run(connection, scan_record)
            _insert_symbol_results(connection, symbol_records)
            _insert_setup_candidates(connection, setup_records)
            _insert_replay_results(connection, replay_records)
            _store_symbol_health(connection, result, timestamp=timestamp)
            connection.commit()
    except sqlite3.Error as exc:
        raise StorageError(f"Unable to store scan run in database: {database_path}") from exc
    return run_id


def _store_symbol_health(connection: sqlite3.Connection, result: ScannerRunResult, *, timestamp: str) -> None:
    existing_payload = getattr(result, "symbol_health", None)
    health_records = symbol_health_records_from_payload(existing_payload if isinstance(existing_payload, Mapping) else None)
    if not health_records:
        symbols = tuple(symbol_result.symbol for symbol_result in result.results)
        existing_records = _load_symbol_health_records(connection, symbols)
        health_records = update_symbol_health_records(
            existing_records,
            result.results,
            now=timestamp,
        )
    _upsert_symbol_health_records(connection, health_records.values())
    scan_run_id = result.resume_metadata.get("scan_run_id") if isinstance(result.resume_metadata, Mapping) else None
    _insert_symbol_health_events(
        connection,
        symbol_health_events_from_results(result.results, now=timestamp),
        scan_run_id=scan_run_id,
    )


def list_scan_history(
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    *,
    limit: int = 10,
) -> tuple[ScanHistorySummary, ...]:
    limit = max(1, int(limit))
    try:
        with closing(open_read_only_database(database_path)) as connection:
            rows = connection.execute(
                """
                SELECT run_id, timestamp, universe, symbols_scanned, total_valid_setups,
                       near_misses, rejected, data_issues, market_regime, regime_confidence, runtime_stats_json,
                       actionable_setups, actionable_a_grade_setups, actionable_a_grade_target_caution, confirmed_setups,
                       candidate_a_grade_setups, blocked_a_grade_by_scoring,
                       blocked_a_grade_by_target, blocked_a_grade_by_entry_window,
                       blocked_a_grade_by_trust, fatal_target_blocks, soft_target_warnings
                FROM scan_runs
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except DatabaseMissingError:
        raise
    except sqlite3.Error as exc:
        raise StorageError(f"Unable to read scan history database: {database_path}") from exc

    return tuple(_history_summary_from_row(row) for row in rows)


def export_history_payload(
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    return [item.to_json() for item in list_scan_history(database_path, limit=limit)]


def format_history_table(history: Sequence[ScanHistorySummary]) -> str:
    if not history:
        return "No scan history found."

    headers = (
        "timestamp",
        "universe",
        "symbols",
        "valid",
        "near_miss",
        "rejected",
        "regime",
        "runtime",
    )
    rows = [
        (
            item.timestamp,
            item.universe,
            str(item.symbols_scanned),
            str(item.total_valid_setups),
            str(item.near_misses),
            str(item.rejected),
            item.market_regime,
            item.runtime_seconds,
        )
        for item in history
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    lines = [
        " | ".join(headers[index].ljust(widths[index]) for index in range(len(headers))),
        "-+-".join("-" * width for width in widths),
    ]
    lines.extend(" | ".join(row[index].ljust(widths[index]) for index in range(len(headers))) for row in rows)
    return "\n".join(lines)


def _scan_run_record(
    *,
    run_id: str,
    timestamp: str,
    result: ScannerRunResult,
    command_preset: str | None,
    command_used: str | None,
    raw_payload: Mapping[str, Any],
    watch_iteration: WatchIterationMetadata | None,
) -> ScanRunRecord:
    counts = _bucket_counts(raw_payload)
    lifecycle_counts = _lifecycle_state_counts(raw_payload, result)
    a_grade_counts = _a_grade_actionability_counts(raw_payload, result)
    actionable_a_grade_setups = a_grade_counts["actionable_a_grade_setups"]
    confirmed_setups = lifecycle_counts.get("CONFIRMED", 0)
    data_issues = _data_issues(result)
    watch_data_issues = watch_iteration.data_issues if watch_iteration is not None else counts["data_issue"]
    summary = _scan_summary_metadata(result, watch_iteration=watch_iteration)
    return ScanRunRecord(
        run_id=run_id,
        timestamp=timestamp,
        exchange=result.config.exchange,
        universe=_universe_label(result),
        symbols_scanned=result.scanned_symbols,
        symbols_json=_json_dump([symbol.symbol for symbol in result.config.symbols]),
        strategy=_display(result.config.strategy_name),
        timeframes_json=_json_dump(
            {
                "htf": result.config.htf_timeframe,
                "bias": result.config.bias_timeframe,
                "execution": result.config.execution_timeframe,
                "confirmation": result.config.confirmation_timeframe,
            }
        ),
        market_regime=_display(result.market_regime.state.value),
        regime_confidence=int(result.market_regime.confidence_score),
        regime_compatibility_json=_json_dump(
            {mode: item.model_dump(mode="json") for mode, item in result.market_regime.compatibility_scores.items()}
        ),
        environment_notes_json=_json_dump(result.market_regime.environment_notes),
        runtime_stats_json=_json_dump(_runtime_stats_payload(result)),
        command_preset=_display(command_preset),
        command_used=_display(command_used),
        total_valid_setups=counts["valid"],
        near_misses=counts["near_miss"],
        rejected=counts["no_setup"],
        data_issues=watch_data_issues,
        data_issues_json=_json_dump(data_issues),
        raw_payload_json=_json_dump(raw_payload),
        is_watch_iteration=1 if watch_iteration is not None else 0,
        watch_iteration_number=watch_iteration.iteration_number if watch_iteration is not None else None,
        started_at=watch_iteration.started_at if watch_iteration is not None else None,
        completed_at=watch_iteration.completed_at if watch_iteration is not None else None,
        symbols_requested=summary["symbols_requested"],
        symbols_queued=summary["symbols_queued"],
        symbols_completed=summary["symbols_completed"],
        valid_activations=watch_iteration.valid_activations if watch_iteration is not None else 0,
        still_watching=watch_iteration.still_watching if watch_iteration is not None else 0,
        rejected_no_edge=watch_iteration.rejected_no_edge if watch_iteration is not None else 0,
        runtime_sec=summary["runtime_sec"],
        portfolio_summary_json=_json_dump(watch_iteration.portfolio_summary or {})
        if watch_iteration is not None
        else "{}",
        symbol_health_summary_json=_json_dump(watch_iteration.symbol_health_summary or {})
        if watch_iteration is not None
        else "{}",
        actionable_setups=actionable_a_grade_setups,
        actionable_a_grade_setups=actionable_a_grade_setups,
        actionable_a_grade_target_caution=a_grade_counts["actionable_a_grade_target_caution"],
        confirmed_setups=confirmed_setups,
        candidate_a_grade_setups=a_grade_counts["candidate_a_grade_setups"],
        blocked_a_grade_by_scoring=a_grade_counts["blocked_a_grade_by_scoring"],
        blocked_a_grade_by_target=a_grade_counts["blocked_a_grade_by_target"],
        blocked_a_grade_by_entry_window=a_grade_counts["blocked_a_grade_by_entry_window"],
        blocked_a_grade_by_trust=a_grade_counts["blocked_a_grade_by_trust"],
        fatal_target_blocks=a_grade_counts["fatal_target_blocks"],
        soft_target_warnings=a_grade_counts["soft_target_warnings"],
    )


def _scan_summary_metadata(
    result: ScannerRunResult,
    *,
    watch_iteration: WatchIterationMetadata | None,
) -> dict[str, int | float]:
    metadata = result.resume_metadata if isinstance(result.resume_metadata, Mapping) else {}
    requested = _metadata_symbol_count(metadata, "watchlist_symbols")
    queued = _metadata_symbol_count(metadata, "symbols_to_scan")

    if requested is None:
        requested = len(result.config.symbols)
    if queued is None:
        queued = len(result.config.symbols)

    if watch_iteration is not None:
        requested = _non_negative_int(watch_iteration.symbols_requested)
        queued = _non_negative_int(watch_iteration.symbols_queued)

    runtime = result.runtime_stats
    return {
        "symbols_requested": _non_negative_int(requested),
        "symbols_queued": _non_negative_int(queued),
        "symbols_completed": _non_negative_int(runtime.completed_symbols),
        "runtime_sec": _non_negative_float(runtime.total_runtime_seconds),
    }


def _runtime_stats_payload(result: ScannerRunResult) -> dict[str, Any]:
    payload = result.runtime_stats.model_dump(mode="json")
    metadata = result.resume_metadata if isinstance(result.resume_metadata, Mapping) else {}
    queue_diagnostics = metadata.get("symbol_queue")
    if isinstance(queue_diagnostics, Mapping):
        payload["symbol_queue"] = dict(queue_diagnostics)
    return payload


def _metadata_symbol_count(metadata: Mapping[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, str):
        return None
    if isinstance(value, Sequence):
        return len(value)
    return None


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _non_negative_float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _storage_payload(
    result: ScannerRunResult,
    ranked_by_symbol: Mapping[str, RankedSymbolDisplay],
    replay_summary: ReplaySummary | None,
    portfolio_selection: PortfolioSelectionResult | None,
) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    for raw_result in payload.get("results", []):
        if not isinstance(raw_result, dict):
            continue
        symbol = _display(raw_result.get("symbol"))
        ranked = ranked_by_symbol.get(symbol)
        symbol_result = ranked.symbol_result if ranked is not None else _symbol_result_by_symbol(result, symbol)
        if symbol_result is not None:
            raw_result.update(display_fields(symbol_result, display_rank=ranked.display_rank if ranked is not None else None))
    if replay_summary is not None:
        payload["replay_result"] = replay_summary.model_dump(mode="json")
    if portfolio_selection is not None:
        payload["portfolio_selection"] = portfolio_selection.model_dump(mode="json")
    return payload


def _symbol_result_by_symbol(result: ScannerRunResult, symbol: str) -> ScannerSymbolResult | None:
    for symbol_result in result.results:
        if symbol_result.symbol == symbol:
            return symbol_result
    return None


def _symbol_result_record(
    *,
    run_id: str,
    result: ScannerRunResult,
    symbol_result: ScannerSymbolResult,
    ranked: RankedSymbolDisplay | None,
    portfolio_decision: str,
) -> SymbolResultRecord:
    fields = display_fields(symbol_result, display_rank=ranked.display_rank if ranked is not None else None)
    diagnostics = _representative_diagnostics(symbol_result)
    quality = symbol_result.setup_quality
    raw_result = symbol_result.model_dump(mode="json")
    raw_result.update(fields)
    return SymbolResultRecord(
        run_id=run_id,
        symbol=symbol_result.symbol,
        status=symbol_result.status.value,
        display_bucket=_display(fields.get("display_bucket")),
        readiness_score=int(fields.get("readiness_score") or 0),
        setup_quality_score=_display(getattr(quality, "quality_score", NA)),
        edge_score=_display(
            _first_non_na(
                symbol_result.historical_expectancy,
                symbol_result.expectancy_metrics.get("expectancy") if symbol_result.expectancy_metrics else NA,
                symbol_result.historical_match_summary.get("expectancy") if symbol_result.historical_match_summary else NA,
            )
        ),
        failed_gate=_display(fields.get("failed_gate")),
        rejection_reason=_rejection_reason(symbol_result, diagnostics),
        next_trigger_needed=_display(fields.get("next_trigger_needed")),
        action_label=_display(fields.get("action_label")),
        regime_state=_display(result.market_regime.state.value),
        regime_confidence=_display(result.market_regime.confidence_score),
        regime_compatibility_score=_display(symbol_result.regime_compatibility_score),
        regime_compatibility_label=_display(symbol_result.regime_compatibility_label),
        regime_penalty=int(symbol_result.regime_penalty),
        environment_notes_json=_json_dump(symbol_result.regime_notes or result.market_regime.environment_notes),
        derivatives_context_json=_json_dump(
            {
                "funding_rate": _display(symbol_result.funding_rate),
                "funding_status": _display(symbol_result.funding_status),
                "open_interest": _display(symbol_result.open_interest),
                "open_interest_change_pct": _display(symbol_result.open_interest_change_pct),
                "oi_direction": _display(symbol_result.oi_direction),
                "price_oi_relationship": _display(symbol_result.price_oi_relationship),
                "crowding_risk": _display(symbol_result.crowding_risk),
                "squeeze_risk": _display(symbol_result.squeeze_risk),
                "derivatives_score": _display(symbol_result.derivatives_score),
                "missing_data": list(symbol_result.derivatives_missing_data),
                "unverified_data": list(symbol_result.derivatives_unverified_data),
            }
        ),
        volume_profile_context_json=_json_dump(
            {
                "source": _display(symbol_result.volume_profile_source),
                "poc": _display(symbol_result.poc),
                "value_area_high": _display(symbol_result.value_area_high),
                "value_area_low": _display(symbol_result.value_area_low),
                "nearest_high_volume_node": _display(symbol_result.nearest_high_volume_node),
                "nearest_low_volume_node": _display(symbol_result.nearest_low_volume_node),
                "warnings": list(symbol_result.volume_profile_warnings),
            }
        ),
        pullback_status=_display(diagnostics.get("pullback_zone_status")),
        portfolio_decision=_display(portfolio_decision),
        raw_result_json=_json_dump(raw_result),
    )


def _setup_candidate_record(run_id: str, symbol_result: ScannerSymbolResult) -> SetupCandidateRecord | None:
    lifecycle = getattr(symbol_result, "lifecycle_state", None)
    lifecycle_state = _display(getattr(lifecycle, "current_state", NA))
    a_grade_watch = lifecycle_state in {"ACTIONABLE_A_GRADE", "A_GRADE_WATCH"}
    if not symbol_result.valid_strategy_modes and not a_grade_watch:
        return None
    diagnostics = _representative_diagnostics(symbol_result)
    mode = _display(
        _first_non_na(
            symbol_result.valid_strategy_modes[0] if symbol_result.valid_strategy_modes else NA,
            symbol_result.rejected_strategy_modes[0] if symbol_result.rejected_strategy_modes else NA,
            diagnostics.get("mode"),
        )
    )
    if mode == NA:
        return None
    setup_result = symbol_result.strategy_results.get(mode)
    setup = getattr(setup_result, mode, None) if setup_result is not None else None
    quality = symbol_result.setup_quality
    raw_candidate = setup.model_dump(mode="json") if isinstance(setup, BaseModel) else dict(diagnostics)
    candidate_quality_grade = _display(
        _first_non_na(
            getattr(symbol_result, "candidate_quality_grade", NA),
            getattr(lifecycle, "candidate_quality_grade", NA),
            getattr(quality.quality_grade, "value", quality.quality_grade),
            diagnostics.get("candidate_quality_grade"),
            diagnostics.get("quality_grade"),
        )
    )
    final_quality_grade = _display(
        _first_non_na(
            getattr(symbol_result, "final_quality_grade", NA),
            getattr(lifecycle, "final_quality_grade", NA),
            diagnostics.get("final_quality_grade"),
            candidate_quality_grade,
        )
    )
    technical_score = _display(
        _first_non_na(
            getattr(symbol_result, "technical_score", NA),
            getattr(lifecycle, "technical_score", NA),
            diagnostics.get("technical_score"),
        )
    )
    opportunity_score = _display(
        _first_non_na(
            getattr(getattr(symbol_result, "score_result", None), "total_score", NA),
            getattr(lifecycle, "opportunity_score", NA),
            diagnostics.get("opportunity_score"),
            diagnostics.get("total_score"),
        )
    )
    failed_gate = _display(
        _first_non_na(
            getattr(symbol_result, "final_failed_gate", NA),
            getattr(lifecycle, "final_failed_gate", NA),
            diagnostics.get("final_failed_gate"),
            diagnostics.get("first_failed_gate"),
            getattr(symbol_result, "rejection_stage", NA),
        )
    )
    final_block_reason = _display(
        _first_non_na(
            getattr(symbol_result, "final_block_reason", NA),
            getattr(lifecycle, "final_block_reason", NA),
            diagnostics.get("final_block_reason"),
        )
    )
    target_integrity_status = _display(
        _first_non_na(
            getattr(symbol_result, "target_integrity_status", NA),
            getattr(lifecycle, "target_integrity_status", NA),
            diagnostics.get("target_integrity_status"),
        )
    )
    target_failure = _display(
        _first_non_na(
            getattr(symbol_result, "target_failure", NA),
            getattr(lifecycle, "target_failure", NA),
            diagnostics.get("target_failure"),
            diagnostics.get("target_failure_type"),
            diagnostics.get("target_failure_reason"),
        )
    )
    target_failure_severity = _display(
        _first_non_na(
            getattr(symbol_result, "target_failure_severity", NA),
            getattr(lifecycle, "target_failure_severity", NA),
            diagnostics.get("target_failure_severity"),
        )
    )
    target_warning_reason = _display(
        _first_non_na(
            getattr(symbol_result, "target_warning_reason", NA),
            getattr(lifecycle, "target_warning_reason", NA),
            diagnostics.get("target_warning_reason"),
            diagnostics.get("target_integrity_warning"),
        )
    )
    actionability_state = _display(
        _first_non_na(
            getattr(symbol_result, "actionability_state", NA),
            getattr(lifecycle, "actionability_state", NA),
            diagnostics.get("actionability_state"),
        )
    )
    return SetupCandidateRecord(
        run_id=run_id,
        symbol=symbol_result.symbol,
        mode=mode,
        direction=_display(_first_non_na(getattr(setup, "bias", NA), diagnostics.get("bias"), diagnostics.get("direction"))),
        entry=_display(_first_non_na(getattr(setup, "entry", NA), diagnostics.get("entry"), _entry_zone_text(diagnostics))),
        stop=_display(_first_non_na(getattr(setup, "stop", NA), diagnostics.get("stop"), diagnostics.get("stop_loss"))),
        tp1=_display(_first_non_na(getattr(setup, "tp1", NA), diagnostics.get("tp1"))),
        tp2=_display(_first_non_na(getattr(setup, "tp2", NA), diagnostics.get("tp2"))),
        tp3=_display(_first_non_na(getattr(setup, "tp3", NA), diagnostics.get("tp3"))),
        rr=_display(_first_non_na(getattr(setup, "rr_to_tp2", NA), diagnostics.get("rr_to_tp2"))),
        invalidation=_display(
            _first_non_na(getattr(setup, "invalidation", NA), diagnostics.get("invalidation"), diagnostics.get("invalidation_reason"))
        ),
        quality_grade=_display(getattr(quality.quality_grade, "value", quality.quality_grade)),
        candidate_quality_grade=candidate_quality_grade,
        final_quality_grade=final_quality_grade,
        technical_score=technical_score,
        opportunity_score=opportunity_score,
        failed_gate=failed_gate,
        final_failed_gate=failed_gate,
        final_block_reason=final_block_reason,
        target_integrity_status=target_integrity_status,
        target_failure=target_failure,
        target_failure_severity=target_failure_severity,
        target_warning_reason=target_warning_reason,
        actionability_state=actionability_state,
        trust_meter=_trust_meter_text(setup, diagnostics),
        risk_warning=_display(getattr(setup, "risk_warning", NA)),
        raw_candidate_json=_json_dump(raw_candidate),
    )


def _replay_result_records(
    run_id: str,
    replay_summary: ReplaySummary | None,
    regime: str,
) -> tuple[ReplayResultRecord, ...]:
    if replay_summary is None:
        return ()
    records: list[ReplayResultRecord] = []
    for symbol_result in replay_summary.symbols:
        for trade in symbol_result.trades:
            raw_trade = trade.model_dump(mode="json")
            records.append(
                ReplayResultRecord(
                    run_id=run_id,
                    setup_fingerprint=_setup_fingerprint(raw_trade),
                    outcome=trade.outcome.value,
                    filled=int(bool(trade.entry_filled or trade.filled)),
                    tp_hit=_tp_hit_text(trade),
                    sl_hit=int(bool(trade.sl_hit)),
                    final_r=_display(trade.final_r_multiple),
                    time_in_trade=_display(trade.candles_held or trade.time_to_final_outcome),
                    regime=_display(regime),
                    symbol=trade.symbol,
                    mode=trade.mode.value,
                    raw_result_json=_json_dump(raw_trade),
                )
            )
    return tuple(records)


def _insert_scan_run(connection: sqlite3.Connection, record: ScanRunRecord) -> None:
    params = astuple(record)
    placeholders = ", ".join("?" for _ in params)
    connection.execute(
        f"""
        INSERT INTO scan_runs (
            run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
            strategy, timeframes_json, market_regime, regime_confidence,
            regime_compatibility_json, environment_notes_json, runtime_stats_json,
            command_preset, command_used, total_valid_setups, near_misses, rejected,
            data_issues, data_issues_json, raw_payload_json, is_watch_iteration,
            watch_iteration_number, started_at, completed_at, symbols_requested,
            symbols_queued, symbols_completed, valid_activations, still_watching,
            rejected_no_edge, runtime_sec, portfolio_summary_json, symbol_health_summary_json,
            actionable_setups, actionable_a_grade_setups, actionable_a_grade_target_caution, confirmed_setups,
            candidate_a_grade_setups, blocked_a_grade_by_scoring,
            blocked_a_grade_by_target, blocked_a_grade_by_entry_window,
            blocked_a_grade_by_trust, fatal_target_blocks, soft_target_warnings
        ) VALUES ({placeholders})
        """,
        params,
    )


def _insert_symbol_results(connection: sqlite3.Connection, records: Sequence[SymbolResultRecord]) -> None:
    connection.executemany(
        """
        INSERT INTO symbol_results (
            run_id, symbol, status, display_bucket, readiness_score, setup_quality_score,
            edge_score, failed_gate, rejection_reason, next_trigger_needed, action_label,
            regime_state, regime_confidence, regime_compatibility_score, regime_compatibility_label,
            regime_penalty, environment_notes_json, derivatives_context_json, volume_profile_context_json,
            pullback_status, portfolio_decision, raw_result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [astuple(record) for record in records],
    )


def _insert_setup_candidates(connection: sqlite3.Connection, records: Sequence[SetupCandidateRecord]) -> None:
    connection.executemany(
        """
        INSERT INTO setup_candidates (
            run_id, symbol, mode, direction, entry, stop, tp1, tp2, tp3, rr,
            invalidation, quality_grade, candidate_quality_grade, final_quality_grade,
            technical_score, opportunity_score, failed_gate, final_failed_gate, final_block_reason,
            target_integrity_status, target_failure, target_failure_severity, target_warning_reason,
            actionability_state, trust_meter, risk_warning, raw_candidate_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [astuple(record) for record in records],
    )


def _insert_replay_results(connection: sqlite3.Connection, records: Sequence[ReplayResultRecord]) -> None:
    connection.executemany(
        """
        INSERT INTO replay_results (
            run_id, setup_fingerprint, outcome, filled, tp_hit, sl_hit, final_r,
            time_in_trade, regime, symbol, mode, raw_result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [astuple(record) for record in records],
    )


def _history_summary_from_row(row: sqlite3.Row) -> ScanHistorySummary:
    runtime_stats = _json_loads(row["runtime_stats_json"])
    return ScanHistorySummary(
        run_id=row["run_id"],
        timestamp=row["timestamp"],
        universe=row["universe"],
        symbols_scanned=int(row["symbols_scanned"]),
        total_valid_setups=int(row["total_valid_setups"]),
        near_misses=int(row["near_misses"]),
        rejected=int(row["rejected"]),
        data_issues=int(row["data_issues"]),
        market_regime=row["market_regime"],
        regime_confidence=int(row["regime_confidence"] or 0),
        runtime_seconds=_runtime_text(runtime_stats),
        actionable_setups=int(row["actionable_setups"] or 0),
        actionable_a_grade_setups=int(row["actionable_a_grade_setups"] or 0),
        actionable_a_grade_target_caution=int(row["actionable_a_grade_target_caution"] or 0),
        confirmed_setups=int(row["confirmed_setups"] or 0),
        candidate_a_grade_setups=int(row["candidate_a_grade_setups"] or 0),
        blocked_a_grade_by_scoring=int(row["blocked_a_grade_by_scoring"] or 0),
        blocked_a_grade_by_target=int(row["blocked_a_grade_by_target"] or 0),
        blocked_a_grade_by_entry_window=int(row["blocked_a_grade_by_entry_window"] or 0),
        blocked_a_grade_by_trust=int(row["blocked_a_grade_by_trust"] or 0),
        fatal_target_blocks=int(row["fatal_target_blocks"] or 0),
        soft_target_warnings=int(row["soft_target_warnings"] or 0),
    )


def _bucket_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    counts = {"valid": 0, "near_miss": 0, "no_setup": 0, "data_issue": 0}
    for raw_result in payload.get("results", ()):
        if not isinstance(raw_result, Mapping):
            continue
        bucket = _display(raw_result.get("display_bucket"))
        if bucket in counts:
            counts[bucket] += 1
    return counts


def _lifecycle_state_counts(payload: Mapping[str, Any], result: ScannerRunResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for raw_result in payload.get("results", ()):
        if not isinstance(raw_result, Mapping):
            continue
        state = _display(
            _first_non_na(
                raw_result.get("lifecycle_current_state"),
                _mapping_value(raw_result.get("lifecycle_state"), "current_state"),
            )
        )
        if state != NA:
            counts[state] = counts.get(state, 0) + 1
    if counts:
        return counts
    for symbol_result in result.results:
        state = _display(getattr(getattr(symbol_result, "lifecycle_state", None), "current_state", NA))
        if state != NA:
            counts[state] = counts.get(state, 0) + 1
    return counts


def _a_grade_actionability_counts(payload: Mapping[str, Any], result: ScannerRunResult) -> dict[str, int]:
    counts = {
        "candidate_a_grade_setups": 0,
        "actionable_a_grade_setups": 0,
        "actionable_a_grade_target_caution": 0,
        "blocked_a_grade_by_scoring": 0,
        "blocked_a_grade_by_target": 0,
        "blocked_a_grade_by_entry_window": 0,
        "blocked_a_grade_by_trust": 0,
        "fatal_target_blocks": 0,
        "soft_target_warnings": 0,
    }
    raw_results = tuple(item for item in payload.get("results", ()) if isinstance(item, Mapping))
    for index, symbol_result in enumerate(result.results):
        raw_result = raw_results[index] if index < len(raw_results) else {}
        lifecycle = getattr(symbol_result, "lifecycle_state", None)
        diagnostics = _representative_diagnostics(symbol_result)
        actionability_state = _display(
            _first_non_na(
                getattr(symbol_result, "actionability_state", NA),
                getattr(lifecycle, "actionability_state", NA),
                raw_result.get("actionability_state"),
                _mapping_value(raw_result.get("lifecycle_state"), "actionability_state"),
                diagnostics.get("actionability_state"),
            )
        )
        candidate_grade = _display(
            _first_non_na(
                getattr(symbol_result, "candidate_quality_grade", NA),
                getattr(lifecycle, "candidate_quality_grade", NA),
                raw_result.get("candidate_quality_grade"),
                _mapping_value(raw_result.get("lifecycle_state"), "candidate_quality_grade"),
                getattr(symbol_result.setup_quality.quality_grade, "value", symbol_result.setup_quality.quality_grade),
                diagnostics.get("quality_grade"),
            )
        )
        lifecycle_state = _display(
            _first_non_na(
                getattr(lifecycle, "current_state", NA),
                raw_result.get("lifecycle_current_state"),
                _mapping_value(raw_result.get("lifecycle_state"), "current_state"),
            )
        )
        severity_key = _metric_key(
            _first_non_na(
                getattr(symbol_result, "target_failure_severity", NA),
                getattr(lifecycle, "target_failure_severity", NA),
                raw_result.get("target_failure_severity"),
                _mapping_value(raw_result.get("lifecycle_state"), "target_failure_severity"),
                diagnostics.get("target_failure_severity"),
            )
        )
        state_key = _metric_key(actionability_state)
        is_a_candidate = _is_a_grade(candidate_grade) or actionability_state.startswith("A_GRADE_")
        if is_a_candidate:
            counts["candidate_a_grade_setups"] += 1
        if actionability_state in {"A_GRADE_ACTIONABLE", "A_GRADE_ACTIONABLE_TARGET_CAUTION"}:
            counts["actionable_a_grade_setups"] += 1
            if actionability_state == "A_GRADE_ACTIONABLE_TARGET_CAUTION":
                counts["actionable_a_grade_target_caution"] += 1
        elif actionability_state == "A_GRADE_BLOCKED_BY_SCORING":
            counts["blocked_a_grade_by_scoring"] += 1
        elif actionability_state == "A_GRADE_BLOCKED_BY_TARGET":
            counts["blocked_a_grade_by_target"] += 1
        elif actionability_state == "A_GRADE_BLOCKED_BY_ENTRY_WINDOW":
            counts["blocked_a_grade_by_entry_window"] += 1
        elif actionability_state == "A_GRADE_BLOCKED_BY_TRUST":
            counts["blocked_a_grade_by_trust"] += 1
        elif actionability_state == NA and lifecycle_state == "ACTIONABLE_A_GRADE" and is_a_candidate:
            counts["actionable_a_grade_setups"] += 1
        if severity_key == "fatal_target_failure" or state_key == "a_grade_blocked_by_target":
            counts["fatal_target_blocks"] += 1
        if severity_key in {"soft_target_warning", "target_caution_actionable"} or state_key == "a_grade_actionable_target_caution":
            counts["soft_target_warnings"] += 1
    return counts


def _metric_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _is_a_grade(value: Any) -> bool:
    return _display(value).strip().upper() in {"A-", "A", "A+"}


def _data_issues(result: ScannerRunResult) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for symbol_result in result.results:
        missing = _unique((*symbol_result.missing_data, *symbol_result.strategy_missing_data, *symbol_result.derivatives_missing_data))
        unverified = _unique((*symbol_result.unverified_data, *symbol_result.strategy_unverified_data, *symbol_result.derivatives_unverified_data))
        if missing or unverified or symbol_result.error_message:
            issues.append(
                {
                    "symbol": symbol_result.symbol,
                    "missing_data": missing,
                    "unverified_data": unverified,
                    "error": _display(symbol_result.error_message),
                }
            )
    return issues


def _portfolio_decisions(portfolio_selection: PortfolioSelectionResult | None) -> dict[str, str]:
    if portfolio_selection is None:
        return {}
    decisions: dict[str, str] = {}
    for candidate in (*portfolio_selection.selected_candidates, *portfolio_selection.rejected_candidates):
        decisions[candidate.symbol] = candidate.decision.value
    return decisions


def _representative_diagnostics(symbol_result: ScannerSymbolResult) -> Mapping[str, Any]:
    for mode in symbol_result.valid_strategy_modes:
        diagnostics = symbol_result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for mode in symbol_result.rejected_strategy_modes:
        diagnostics = symbol_result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for diagnostics in symbol_result.strategy_diagnostics.values():
        if isinstance(diagnostics, Mapping):
            return diagnostics
    return {}


def _rejection_reason(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    for value in (
        symbol_result.rejection_reason,
        diagnostics.get("pullback_failure_reason"),
        diagnostics.get("confirmation_bos_choch_reason"),
    ):
        text = _display(value)
        if text != NA:
            return text
    if symbol_result.rejection_reasons:
        return "; ".join(symbol_result.rejection_reasons)
    if symbol_result.error_message:
        return symbol_result.error_message
    return NA


def _entry_zone_text(diagnostics: Mapping[str, Any]) -> str:
    low = _display(_first_non_na(diagnostics.get("entry_low"), _mapping_value(diagnostics.get("entry_zone"), "low")))
    high = _display(_first_non_na(diagnostics.get("entry_high"), _mapping_value(diagnostics.get("entry_zone"), "high")))
    if low == NA and high == NA:
        return NA
    if low == NA:
        return high
    if high == NA or high == low:
        return low
    return f"{low}-{high}"


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key, NA) if isinstance(value, Mapping) else NA


def _trust_meter_text(setup: Any, diagnostics: Mapping[str, Any]) -> str:
    trust_meter = getattr(setup, "trust_meter", None)
    grade = _display(getattr(trust_meter, "grade", diagnostics.get("trust_grade")))
    percentage = _display(getattr(trust_meter, "percentage", diagnostics.get("trust_percentage")))
    if grade == NA and percentage == NA:
        return NA
    if percentage == NA:
        return grade
    if grade == NA:
        return percentage
    return f"{grade} {percentage}"


def _tp_hit_text(trade: Any) -> str:
    highest = int(getattr(trade, "highest_tp_hit", 0) or 0)
    if highest > 0:
        return f"TP{highest}"
    return "N/A"


def _setup_fingerprint(raw_trade: Mapping[str, Any]) -> str:
    candidate = raw_trade.get("candidate") if isinstance(raw_trade.get("candidate"), Mapping) else {}
    condition_key = candidate.get("condition_key") if isinstance(candidate, Mapping) else {}
    fingerprint_payload = {
        "symbol": raw_trade.get("symbol"),
        "mode": raw_trade.get("mode"),
        "direction": raw_trade.get("direction"),
        "detected_at_index": candidate.get("detected_at_index") if isinstance(candidate, Mapping) else NA,
        "entry": raw_trade.get("entry"),
        "stop": raw_trade.get("stop"),
        "condition_key": condition_key,
    }
    return hashlib.sha256(_json_dump(fingerprint_payload).encode("utf-8")).hexdigest()


def _runtime_text(runtime_stats: Mapping[str, Any]) -> str:
    value = runtime_stats.get("total_runtime_seconds")
    if value in (None, "", NA):
        return NA
    try:
        seconds = Decimal(str(value))
    except Exception:
        return str(value)
    if seconds == 0:
        return "0s"
    if seconds < Decimal("1"):
        return f"{seconds:.3f}".rstrip("0").rstrip(".") + "s"
    return f"{seconds:.1f}".rstrip("0").rstrip(".") + "s"


def _universe_label(result: ScannerRunResult) -> str:
    metadata = result.resume_metadata.get("universe") if isinstance(result.resume_metadata, Mapping) else None
    if isinstance(metadata, Mapping):
        mode = _display(metadata.get("mode"))
        preset = _display(metadata.get("preset"))
        if mode != NA and preset != NA:
            return f"{mode}:{preset}"
        if mode != NA:
            return mode
    return NA


def _first_non_na(*values: Any) -> Any:
    for value in values:
        if _display(value) != NA:
            return value
    return NA


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _json_dump(value: Any) -> str:
    return json.dumps(_json_ready(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str) -> Mapping[str, Any]:
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, Mapping) else {}


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _display(value)
    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_ready(item) for item in value]
    return value


def _unique(values: Sequence[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        text = _display(value)
        if text != NA and text not in output:
            output.append(text)
    return output
