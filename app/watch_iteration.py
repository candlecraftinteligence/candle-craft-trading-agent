from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from app.data.dtos import NA
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunResult,
    ScannerSymbolResult,
)


OUTCOME_KEYS = ("evaluated", "rejected", "errored", "timed_out", "not_run")


def not_run_symbol_result(symbol: str, *, reason: str) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.NOT_RUN,
        status_history=(ScannerPipelineStatus.NOT_RUN,),
        error_message=f"Symbol was not run: {reason}.",
        iteration_outcome="not_run",
        not_run_reason=reason,
        rejection_stage=NA,
        rejection_reasons=(),
    )


def queued_symbol_outcome_counts(
    result: ScannerRunResult,
    queued_symbols: Sequence[str],
) -> dict[str, int]:
    queued = tuple(queued_symbols)
    queued_set = set(queued)
    accounted = tuple(item for item in result.results if item.symbol in queued_set)
    accounted_symbols = tuple(item.symbol for item in accounted)
    if len(set(accounted_symbols)) != len(accounted_symbols):
        raise AssertionError("queued symbol accounting contains duplicate outcomes")
    missing = tuple(symbol for symbol in queued if symbol not in set(accounted_symbols))
    unexpected = tuple(symbol for symbol in accounted_symbols if symbol not in queued_set)
    if missing or unexpected:
        raise AssertionError(
            f"queued symbol accounting mismatch: missing={list(missing)} unexpected={list(unexpected)}"
        )
    raw = Counter(item.iteration_outcome for item in accounted)
    counts = {key: int(raw.get(key, 0)) for key in OUTCOME_KEYS}
    if sum(counts.values()) != len(queued):
        raise AssertionError(
            f"queued symbol accounting does not reconcile: queued={len(queued)} outcomes={counts}"
        )
    return counts


def queued_symbol_outcomes(
    result: ScannerRunResult,
    queued_symbols: Sequence[str],
) -> dict[str, dict[str, str]]:
    queued_symbol_outcome_counts(result, queued_symbols)
    by_symbol = {
        item.symbol: item
        for item in result.results
        if item.symbol in set(queued_symbols)
    }
    outcomes: dict[str, dict[str, str]] = {}
    for symbol in queued_symbols:
        item = by_symbol[symbol]
        outcome = str(item.iteration_outcome)
        if outcome == "not_run":
            reason_code = item.not_run_reason
        elif outcome == "timed_out":
            reason_code = item.timeout_status
        elif outcome == "errored":
            reason_code = "scan_error"
        elif outcome == "rejected":
            reason_code = item.rejection_stage if item.rejection_stage != NA else "scanner_gate_rejection"
        else:
            reason_code = "evaluated"
        outcomes[symbol] = {
            "outcome": outcome,
            "reason_code": reason_code,
            "status": getattr(item.status, "value", str(item.status)),
        }
    return outcomes


def scanner_phase_status(result: ScannerRunResult, queued_symbols: Sequence[str]) -> str:
    counts = queued_symbol_outcome_counts(result, queued_symbols)
    if not queued_symbols:
        return "SUCCESS"
    failures = counts["errored"] + counts["timed_out"] + counts["not_run"]
    if failures == 0:
        return "SUCCESS"
    useful = counts["evaluated"] + counts["rejected"]
    return "PARTIAL" if useful else "FAILED"


def telegram_outbox_status_summary(summary: Any | None) -> dict[str, int]:
    if summary is None:
        return {}
    counts = {
        "sent": int(getattr(summary, "sent", 0)),
        "skipped": int(getattr(summary, "skipped", 0)),
        "duplicate": int(getattr(summary, "duplicate", 0)),
        "blocked": int(getattr(summary, "blocked", 0)),
        "blocked_repeat": int(getattr(summary, "blocked_repeat", 0)),
        "failed": int(getattr(summary, "failed", 0)),
        "retryable": 0,
        "uncertain": 0,
        "failed_final": 0,
    }
    confirmed_audit = getattr(summary, "confirmed_alert_audit", None)
    if confirmed_audit is not None:
        candidates = int(getattr(confirmed_audit, "confirmed_candidates_seen", 0))
        passed = int(getattr(confirmed_audit, "confirmed_prefilter_passed", 0))
        policy_disabled = int(getattr(confirmed_audit, "confirmed_policy_disabled", 0))
        counts.update(
            {
                "confirmed_transitions": candidates,
                "public_confirmed_candidates": candidates,
                "public_confirmed_prefilter_passed": passed,
                "public_confirmed_rejected_pretransport": max(
                    0,
                    candidates - passed - policy_disabled,
                ),
                "public_confirmed_attempt_records": int(
                    getattr(confirmed_audit, "signal_confirmed_attempts_created", 0)
                ),
                "public_confirmed_sent": int(
                    getattr(confirmed_audit, "signal_confirmed_sent", 0)
                ),
            }
        )
    for delivery in tuple(getattr(summary, "deliveries", ()) or ()):
        status = str(getattr(delivery, "status", "")).strip().lower()
        detail = str(getattr(delivery, "detail", "")).upper()
        if status == "retryable" or "RETRYABLE" in detail:
            counts["retryable"] += 1
        elif status == "uncertain" or "UNCERTAIN" in detail:
            counts["uncertain"] += 1
        elif "FAILED_FINAL" in detail or status == "failed":
            counts["failed_final"] += 1
    return counts


def telegram_outbox_phase_status(counts: Mapping[str, int]) -> str:
    failures = sum(
        int(counts.get(key, 0))
        for key in ("failed", "retryable", "uncertain", "failed_final")
    )
    return "PARTIAL" if failures else "SUCCESS"


def watch_phase_error(phase: str, exc: Exception | SystemExit) -> str:
    detail = str(exc).strip() or type(exc).__name__
    return f"{phase}:{type(exc).__name__}:{detail}"


__all__ = [
    "OUTCOME_KEYS",
    "not_run_symbol_result",
    "queued_symbol_outcome_counts",
    "queued_symbol_outcomes",
    "scanner_phase_status",
    "telegram_outbox_phase_status",
    "telegram_outbox_status_summary",
    "watch_phase_error",
]
