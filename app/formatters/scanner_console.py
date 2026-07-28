"""Human-readable console presentation for scanner watch iterations.

This module intentionally consumes completed scanner/watch data.  It does not
participate in signal selection, lifecycle handling, scheduling, persistence,
or delivery decisions.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, TextIO

from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display
from app.pipeline.scanner_runner import ScannerSymbolResult


ConsoleMode = Literal["compact", "verbose"]

_PHASES: tuple[tuple[str, str], ...] = (
    ("universe", "Universe"),
    ("scanner", "Scanner"),
    ("lifecycle", "Lifecycle"),
    ("queue", "Queue"),
    ("scan_storage", "Database"),
    ("telegram_outbox", "Telegram"),
    ("symbol_health", "Symbol health"),
    ("watch_state", "Watch state"),
)
_OUTCOME_KEYS = ("evaluated", "rejected", "errored", "timed_out", "not_run")
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(telegram[ _-]?(?:bot[ _-]?)?token|bot[ _-]?token|chat[ _-]?id|api[ _-]?key|secret|password)\b\s*[:=]\s*\S+"
)
_TRAILING_CANDLE_COUNT = re.compile(r"\s*[:(]?\s*(\d+)\s*/\s*(\d+)\)?\s*$")


@dataclass(frozen=True)
class _WarningGroup:
    reason: str
    entries: tuple[tuple[str, str], ...]


class ScannerConsolePresenter:
    """Render completed watch iterations in compact or diagnostic form."""

    def __init__(
        self,
        *,
        mode: ConsoleMode,
        stream: TextIO | None = None,
        width: int | None = None,
        color: bool | None = None,
    ) -> None:
        self.mode = mode
        self.stream = stream or sys.stdout
        self.width = _console_width(self.stream, width)
        # The current presentation deliberately uses readable text markers rather
        # than ANSI styling.  Keeping this decision explicit makes redirected
        # PowerShell output and NO_COLOR behaviour deterministic.
        self.color_enabled = bool(color) and _ansi_supported(self.stream)

    @property
    def compact(self) -> bool:
        return self.mode == "compact"

    def emit(self, text: str) -> None:
        print(text, file=self.stream, flush=True)

    def format_watch_startup(
        self,
        *,
        source_label: str,
        queued_symbols: int,
        lifecycle_alerts: str,
        admin_drafts: str,
        legacy_alerts: str,
        warnings: Sequence[str] = (),
    ) -> str:
        if self.compact:
            return ""
        lines = [
            "SCANNER CONSOLE | VERBOSE WATCH STARTUP",
            f"Watchlist: {_safe_text(source_label)}",
            f"Symbols queued: {queued_symbols}",
            f"Telegram lifecycle setup alerts: {_safe_text(lifecycle_alerts)}",
            f"Telegram admin drafts: {_safe_text(admin_drafts)}",
            f"Legacy scanner alerts: {_safe_text(legacy_alerts)}",
        ]
        lines.extend(f"Warning: {_safe_text(item)}" for item in warnings)
        return self._format_lines(lines)

    def format_watch_iteration(
        self,
        summary: Any,
        *,
        results: Sequence[ScannerSymbolResult] = (),
        telegram_deliveries: Sequence[Any] = (),
    ) -> str:
        if self.mode == "verbose":
            return self._format_verbose_iteration(
                summary,
                results=results,
                telegram_deliveries=telegram_deliveries,
            )
        return self._format_compact_iteration(
            summary,
            results=results,
            telegram_deliveries=telegram_deliveries,
        )

    def format_watch_shutdown(
        self,
        *,
        completed_iterations: int,
        stored_scan_runs: int,
        database_path: str,
    ) -> str:
        lines = [
            "=" * self.width,
            " CANDLE CRAFT INTELLIGENCE | SCANNER STOPPED",
            "-" * self.width,
            f"Completed iterations: {completed_iterations} | Stored scan runs: {stored_scan_runs}",
            f"Database: {_safe_text(database_path)}",
            "ACTION",
            "Scanner stopped.",
            "=" * self.width,
        ]
        return self._format_lines(lines)

    def scanner_logger(self):
        """Return a quiet logger for compact mode's already-summarized errors."""
        if not self.compact:
            return None
        import logging

        logger = logging.getLogger("candlecraft.console.compact_scanner")
        if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
            logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return logger

    def _format_compact_iteration(
        self,
        summary: Any,
        *,
        results: Sequence[ScannerSymbolResult],
        telegram_deliveries: Sequence[Any],
    ) -> str:
        status = _safe_text(getattr(summary, "status", "N/A")).upper()
        started = _short_timestamp(getattr(summary, "actual_start", None) or getattr(summary, "scanned_at", None))
        lines = [
            "=" * self.width,
            f" CANDLE CRAFT INTELLIGENCE | SCAN ITERATION {getattr(summary, 'iteration', 'N/A')}",
            f" Started: {started} | Status: {status} | Duration: {_duration(getattr(summary, 'duration_seconds', 0))}",
            "-" * self.width,
            " PIPELINE",
        ]
        lines.extend(self._pipeline_lines(summary))

        lines.extend(("", " RESULTS"))
        lines.extend(self._result_lines(summary))

        candidate_rows = _candidate_rows(summary, results, telegram_deliveries)
        valid_rows = [row for row in candidate_rows if row["valid"]]
        if valid_rows:
            lines.extend(("", " SETUPS"))
            lines.extend(self._setup_table(candidate_rows))
        else:
            lines.append(" No valid setup this iteration.")
            if candidate_rows:
                lines.extend(("", " WATCH"))
                lines.extend(self._setup_table(candidate_rows))

        warning_groups = _warning_groups(summary, results)
        failure_groups = _failure_groups(results)
        if warning_groups or failure_groups or status in {"PARTIAL", "FAILED", "FATAL", "CANCELLED"}:
            lines.extend(("", " WARNINGS"))
            lines.extend(self._warning_lines(warning_groups, marker="[WARN]"))
            lines.extend(self._warning_lines(failure_groups, marker="[FAIL]"))
            if status in {"FAILED", "FATAL", "CANCELLED"} and not failure_groups:
                lines.append(f" [FAIL] Iteration status: {status}")
            if not warning_groups and not failure_groups and status == "PARTIAL":
                lines.append(" [WARN] A recoverable pipeline phase reported a partial result.")

        lines.extend(("", " SCHEDULE"))
        lines.extend(self._schedule_lines(summary))
        lines.extend(("", " ACTION", f" {_action_text(summary)}", "=" * self.width))
        return self._format_lines(lines)

    def _format_verbose_iteration(
        self,
        summary: Any,
        *,
        results: Sequence[ScannerSymbolResult],
        telegram_deliveries: Sequence[Any],
    ) -> str:
        lines = [
            "=" * self.width,
            f" CANDLE CRAFT INTELLIGENCE | SCAN ITERATION {getattr(summary, 'iteration', 'N/A')} | VERBOSE",
            "-" * self.width,
            f"Status: {_safe_text(getattr(summary, 'status', 'N/A')).upper()}",
            f"Iteration ID: {_safe_text(getattr(summary, 'iteration_id', NA))}",
            "TIMING",
            f"  Scheduled start: {_safe_text(getattr(summary, 'scheduled_start', NA))}",
            f"  Actual start: {_safe_text(getattr(summary, 'actual_start', NA))}",
            f"  Finished: {_safe_text(getattr(summary, 'finished_at', NA))}",
            f"  Duration: {_duration(getattr(summary, 'duration_seconds', 0))}",
            f"  Sleep: {_duration(getattr(summary, 'sleep_seconds', 0))}",
            f"  Cadence lag: {_duration(getattr(summary, 'cadence_lag_seconds', 0))}",
            f"  Overrun: {_duration(getattr(summary, 'overrun_seconds', 0))}",
            f"  Missed intervals: {getattr(summary, 'missed_interval_count', 0)}",
            f"  Failure streak/backoff: {getattr(summary, 'consecutive_failure_count', 0)}/{_duration(getattr(summary, 'selected_backoff_seconds', 0))}",
            f"  Next scheduled attempt: {_safe_text(getattr(summary, 'next_scheduled_attempt', NA))}",
            "OUTCOME COUNTS",
        ]
        outcome_counts = _mapping(getattr(summary, "outcome_counts", {}))
        for key in _OUTCOME_KEYS:
            lines.append(f"  {key}: {_count(outcome_counts, key)}")
        lines.extend(
            (
                f"  queue_total: {getattr(summary, 'queue_total', 0)}",
                f"  symbols_watched: {getattr(summary, 'symbols_watched', 0)}",
                f"  valid_activations: {getattr(summary, 'valid_activations', 0)}",
                f"  still_watching: {getattr(summary, 'still_watching', 0)}",
                f"  rejected_no_edge: {getattr(summary, 'rejected_no_edge', 0)}",
                f"  data_issues: {getattr(summary, 'data_issues', 0)}",
                "PHASES",
            )
        )
        for key, value in sorted(_mapping(getattr(summary, "phase_statuses", {})).items()):
            lines.append(f"  {key}: {_safe_text(value)}")
        lines.append(f"  database_storage: {_safe_text(getattr(summary, 'database_storage_status', NA))}")
        lines.append("TELEGRAM OUTBOX")
        outbox = _mapping(getattr(summary, "telegram_outbox_status", {}))
        if outbox:
            for key, value in sorted(outbox.items()):
                lines.append(f"  {key}: {_safe_text(value)}")
        else:
            lines.append("  N/A")
        lines.append("SYMBOL OUTCOMES")
        outcomes = _mapping(getattr(summary, "symbol_outcomes", {}))
        if outcomes:
            for symbol in sorted(outcomes):
                item = _mapping(outcomes[symbol])
                lines.append(
                    f"  {symbol}: outcome={_safe_text(item.get('outcome', NA))}; "
                    f"reason={_safe_text(item.get('reason_code', NA))}; "
                    f"status={_safe_text(item.get('status', NA))}"
                )
        else:
            lines.append("  N/A")
        lines.append("SYMBOL DIAGNOSTICS")
        if results:
            for result in sorted(results, key=lambda item: item.symbol):
                display = build_symbol_display(result)
                reason = _result_reason(result, display_status=display.display_status)
                lines.append(
                    f"  {result.symbol}: status={display.display_status}; outcome={result.iteration_outcome}; "
                    f"reason={reason}"
                )
        else:
            lines.append("  N/A")
        if telegram_deliveries:
            lines.append("TELEGRAM DELIVERIES")
            for delivery in sorted(telegram_deliveries, key=lambda item: _safe_text(getattr(item, "symbol", NA))):
                lines.append(
                    f"  {_safe_text(getattr(delivery, 'symbol', NA))}: "
                    f"{_safe_text(getattr(delivery, 'status', NA))}; "
                    f"{_safe_text(getattr(delivery, 'detail', NA))}"
                )
        errors = tuple(getattr(summary, "errors", ()) or ())
        if errors:
            lines.append("RECOVERABLE ERRORS")
            lines.extend(f"  {_safe_text(error)}" for error in errors)
        lines.extend(("ACTION", f"  {_action_text(summary)}", "=" * self.width))
        return self._format_lines(lines)

    def _pipeline_lines(self, summary: Any) -> list[str]:
        phases = {key: _safe_text(value).upper() for key, value in _mapping(getattr(summary, "phase_statuses", {})).items()}
        items: list[str] = []
        if "iteration" in phases:
            items.append(_phase_item("Iteration", phases["iteration"]))
        for key, label in _PHASES:
            phase_value = phases.get(key)
            if key == "scan_storage":
                phase_value = _safe_text(getattr(summary, "database_storage_status", phase_value or "NOT_REQUESTED")).upper()
            if phase_value is None:
                phase_value = "NOT_REPORTED"
            items.append(_phase_item(label, phase_value))
        return _pack_items(items, width=self.width, indent=" ")

    def _result_lines(self, summary: Any) -> list[str]:
        outcomes = _mapping(getattr(summary, "outcome_counts", {}))
        failures = sum(_count(outcomes, key) for key in ("errored", "timed_out", "not_run"))
        first = (
            f" Symbols watched: {getattr(summary, 'symbols_watched', 0)} | "
            f"Queued: {getattr(summary, 'queue_total', 0)} | "
            f"Evaluated: {_count(outcomes, 'evaluated')}"
        )
        second = (
            f" No setup: {getattr(summary, 'rejected_no_edge', 0)} | "
            f"Data warnings: {getattr(summary, 'data_issues', 0)} | Failed: {failures}"
        )
        outbox = _mapping(getattr(summary, "telegram_outbox_status", {}))
        third = (
            f" Valid activations: {getattr(summary, 'valid_activations', 0)} | "
            f"Still watching: {getattr(summary, 'still_watching', 0)} | "
            f"Signals sent: {_count(outbox, 'sent')} | Repeats blocked: {_count(outbox, 'blocked_repeat')}"
        )
        return [first, second, third]

    def _warning_lines(self, groups: Sequence[_WarningGroup], *, marker: str) -> list[str]:
        lines: list[str] = []
        for group in groups:
            count = len(group.entries)
            lines.append(f" {marker} {group.reason}: {count} symbol{'s' if count != 1 else ''}")
            examples = group.entries[:8]
            examples_text = " | ".join(
                symbol if not detail else f"{symbol} {detail}" for symbol, detail in examples
            )
            if examples_text:
                lines.append(f"   {examples_text}")
            remaining = count - len(examples)
            if remaining:
                lines.append(f"   ...and {remaining} more")
        return lines

    def _schedule_lines(self, summary: Any) -> list[str]:
        cadence_seconds = float(getattr(summary, "duration_seconds", 0) or 0) + float(
            getattr(summary, "sleep_seconds", 0) or 0
        )
        return [
            f" Cadence: {_duration(cadence_seconds)} | Overrun: {_duration(getattr(summary, 'overrun_seconds', 0))} | "
            f"Missed intervals: {getattr(summary, 'missed_interval_count', 0)}",
            f" Next attempt: {_short_timestamp(getattr(summary, 'next_scheduled_attempt', NA))}",
        ]

    def _setup_table(self, rows: Sequence[dict[str, str | bool]]) -> list[str]:
        headings = ("Symbol", "Mode", "Side", "Grade", "Score", "State", "RR", "Telegram")
        cells = [
            (
                str(row["symbol"]),
                str(row["mode"]),
                str(row["side"]),
                str(row["grade"]),
                str(row["score"]),
                str(row["state"]),
                str(row["rr"]),
                str(row["telegram"]),
            )
            for row in rows
        ]
        max_widths = (9, 8, 5, 5, 5, 9, 5, 8)
        widths = tuple(
            min(limit, max(len(heading), *(len(_truncate(cell[index], limit)) for cell in cells)))
            for index, (heading, limit) in enumerate(zip(headings, max_widths, strict=True))
        )
        table_width = sum(widths) + 3 * (len(headings) - 1) + 1
        if table_width > self.width - 2:
            lines: list[str] = []
            for row in cells:
                detail = " | ".join(f"{heading}: {value}" for heading, value in zip(headings, row, strict=True))
                lines.append(f" {detail}")
            return lines
        header = " " + " | ".join(heading.ljust(widths[index]) for index, heading in enumerate(headings))
        divider = " " + "-+-".join("-" * width for width in widths)
        lines = [header, divider]
        for row in cells:
            lines.append(
                " "
                + " | ".join(
                    _truncate(value, widths[index]).ljust(widths[index])
                    for index, value in enumerate(row)
                )
            )
        return lines

    def _format_lines(self, lines: Iterable[str]) -> str:
        output: list[str] = []
        for line in lines:
            if line and set(line) <= {"=", "-"}:
                output.append(line[: self.width])
                continue
            output.extend(_wrap_line(line, width=self.width))
        return "\n".join(output)


def format_watch_iteration_console(
    summary: Any,
    *,
    mode: ConsoleMode = "compact",
    results: Sequence[ScannerSymbolResult] = (),
    telegram_deliveries: Sequence[Any] = (),
    width: int | None = None,
    stream: TextIO | None = None,
) -> str:
    """Format a completed watch iteration without writing to the console."""
    return ScannerConsolePresenter(mode=mode, stream=stream, width=width).format_watch_iteration(
        summary,
        results=results,
        telegram_deliveries=telegram_deliveries,
    )


def _candidate_rows(
    summary: Any,
    results: Sequence[ScannerSymbolResult],
    telegram_deliveries: Sequence[Any],
) -> list[dict[str, str | bool]]:
    telegram_by_symbol: dict[str, str] = {}
    for delivery in telegram_deliveries:
        symbol = _safe_text(getattr(delivery, "symbol", NA)).upper()
        if symbol != NA:
            telegram_by_symbol[symbol] = _safe_text(getattr(delivery, "status", NA)).upper()
    for activation in tuple(getattr(summary, "alerts", ()) or ()):
        symbol = _safe_text(getattr(activation, "symbol", NA)).upper()
        if symbol != NA:
            telegram_by_symbol[symbol] = _safe_text(getattr(activation, "delivery_status", NA)).upper()

    rows: list[dict[str, str | bool]] = []
    for result in results:
        display = build_symbol_display(result)
        lifecycle_state = getattr(getattr(result, "lifecycle_state", None), "current_state", NA)
        lifecycle_text = _safe_text(getattr(lifecycle_state, "value", lifecycle_state))
        is_watch_state = any(token in lifecycle_text.lower() for token in ("watch", "stalk", "trigger"))
        if display.display_bucket not in {"valid", "near_miss"} and not is_watch_state:
            continue
        trade_idea = result.trade_idea
        score_result = result.score_result
        mode = _first_text(result.valid_strategy_modes) or _first_text(result.rejected_strategy_modes) or NA
        grade = _safe_text(getattr(score_result, "grade", getattr(trade_idea, "grade", NA)))
        score = _number_text(getattr(score_result, "total_score", getattr(trade_idea, "confidence_score", NA)))
        side = _safe_text(getattr(trade_idea, "direction", NA))
        rr = _number_text(getattr(trade_idea, "best_rr", NA))
        state = lifecycle_text if lifecycle_text != NA else _safe_text(display.readiness_label)
        rows.append(
            {
                "symbol": result.symbol,
                "mode": mode,
                "side": side,
                "grade": grade,
                "score": score,
                "state": state,
                "rr": rr,
                "telegram": telegram_by_symbol.get(result.symbol.upper(), NA),
                "valid": display.display_bucket == "valid",
            }
        )
    return rows


def _warning_groups(summary: Any, results: Sequence[ScannerSymbolResult]) -> tuple[_WarningGroup, ...]:
    grouped: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
    for result in results:
        display = build_symbol_display(result)
        if display.display_status not in {"data_issue", "scan_error"}:
            continue
        if result.iteration_outcome in {"errored", "timed_out"}:
            continue
        reason = _result_reason(result, display_status=display.display_status)
        _append_warning(grouped, reason, result.symbol)
    for error in tuple(getattr(summary, "errors", ()) or ()):
        error_text = _safe_text(error)
        if error_text:
            _append_warning(grouped, error_text, "iteration")
    return tuple(
        _WarningGroup(reason=reason, entries=tuple(entries))
        for reason, entries in sorted(grouped.items(), key=lambda item: item[0].casefold())
    )


def _failure_groups(results: Sequence[ScannerSymbolResult]) -> tuple[_WarningGroup, ...]:
    grouped: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
    for result in results:
        if result.iteration_outcome not in {"errored", "timed_out", "not_run"}:
            continue
        if result.iteration_outcome == "not_run":
            reason = f"Not run: {_safe_text(result.not_run_reason)}"
        elif result.iteration_outcome == "timed_out":
            reason = _result_reason(result, display_status="scan_error")
        else:
            reason = _result_reason(result, display_status="scan_error")
        _append_warning(grouped, reason, result.symbol)
    return tuple(
        _WarningGroup(reason=reason, entries=tuple(entries))
        for reason, entries in sorted(grouped.items(), key=lambda item: item[0].casefold())
    )


def _append_warning(grouped: OrderedDict[str, list[tuple[str, str]]], reason: str, symbol: str) -> None:
    normalized, detail = _normalize_warning_reason(reason)
    grouped.setdefault(normalized, []).append((symbol, detail))


def _normalize_warning_reason(reason: str) -> tuple[str, str]:
    safe = _safe_text(reason)
    match = _TRAILING_CANDLE_COUNT.search(safe)
    if match is None:
        return safe, ""
    normalized = safe[: match.start()].rstrip(" :(") or safe
    return normalized, f"{match.group(1)}/{match.group(2)}"


def _result_reason(result: ScannerSymbolResult, *, display_status: str) -> str:
    values: list[Any] = [
        result.error_message,
        result.rejection_reason,
        *result.missing_data,
        *result.strategy_missing_data,
        *result.derivatives_missing_data,
        *getattr(result.setup_quality, "missing_data", ()),
    ]
    for value in values:
        text = _safe_text(value)
        if text != NA:
            return text
    if result.iteration_outcome == "timed_out":
        return f"Timed out: {_safe_text(result.timeout_status)}"
    if result.iteration_outcome == "not_run":
        return f"Not run: {_safe_text(result.not_run_reason)}"
    if display_status == "data_issue":
        return "Required market data is missing or unavailable"
    return "Scanner error"


def _phase_item(label: str, value: str) -> str:
    normalized = value.upper()
    if normalized in {"FAILED", "FATAL", "CANCELLED"}:
        marker = "[FAIL]"
    elif normalized in {"PARTIAL", "NOT_REPORTED"}:
        marker = "[WARN]"
    else:
        marker = "[OK]"
    suffix = ""
    if normalized in {"SKIPPED", "NOT_REQUESTED", "NOT_ATTEMPTED"}:
        suffix = f" ({normalized.lower().replace('_', ' ')})"
    return f"{marker} {label}{suffix}"


def _action_text(summary: Any) -> str:
    status = _safe_text(getattr(summary, "status", "N/A")).upper()
    next_seconds = getattr(summary, "next_scan_seconds", None)
    next_attempt = _short_timestamp(getattr(summary, "next_scheduled_attempt", NA))
    outbox = _mapping(getattr(summary, "telegram_outbox_status", {}))
    sent = _count(outbox, "sent")
    repeats = _count(outbox, "blocked_repeat")
    if status in {"FATAL", "CANCELLED"}:
        return "Iteration failed and scanner stopped."
    if status == "FAILED":
        if next_seconds is not None and float(next_seconds) > 0:
            return f"Iteration failed and will retry at {next_attempt}."
        return "Iteration failed; scanner stopped."
    if next_seconds is None or float(next_seconds) <= 0:
        return "Configured watch limit reached; scanner stopped."
    parts: list[str] = []
    if sent:
        parts.append(f"{sent} public Telegram signal{' was' if sent == 1 else 's were'} sent")
    else:
        parts.append("No public signal was sent")
    if repeats:
        parts.append(f"{repeats} repeated alert{' was' if repeats == 1 else 's were'} safely blocked")
    if status == "PARTIAL":
        parts.append("partial scan has recoverable warnings")
    parts.append(f"waiting for next scan at {next_attempt}")
    return ". ".join(parts) + "."


def _pack_items(items: Sequence[str], *, width: int, indent: str) -> list[str]:
    lines: list[str] = []
    current = indent
    for item in items:
        separator = "" if current == indent else "  "
        candidate = current + separator + item
        if current != indent and len(candidate) > width:
            lines.extend(_wrap_line(current.rstrip(), width=width))
            current = indent + item
            continue
        current = candidate
    if current != indent:
        lines.extend(_wrap_line(current.rstrip(), width=width))
    return lines


def _console_width(stream: TextIO, explicit_width: int | None) -> int:
    if explicit_width is not None:
        return max(40, int(explicit_width))
    if not _is_tty(stream):
        return 80
    return max(40, shutil.get_terminal_size(fallback=(80, 24)).columns)


def _ansi_supported(stream: TextIO) -> bool:
    return _is_tty(stream) and not os.environ.get("NO_COLOR")


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _wrap_line(line: str, *, width: int) -> list[str]:
    if not line:
        return [""]
    leading = line[: len(line) - len(line.lstrip())]
    text = line.lstrip()
    available = max(20, width - len(leading))
    return [
        leading + part
        for part in textwrap.wrap(
            text,
            width=available,
            break_long_words=True,
            break_on_hyphens=False,
        )
    ] or [leading]


def _short_timestamp(value: Any) -> str:
    text = _safe_text(value)
    if text == NA:
        return NA
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:8] if len(text) >= 8 else text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%H:%M:%S")


def _duration(value: Any) -> str:
    try:
        seconds = max(0, round(float(value)))
    except (TypeError, ValueError):
        seconds = 0
    hours, remaining = divmod(seconds, 3600)
    minutes, seconds = divmod(remaining, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(values: Mapping[str, Any], key: str) -> int:
    try:
        return int(values.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _first_text(values: Sequence[Any]) -> str:
    for value in values:
        text = _safe_text(value)
        if text != NA:
            return text
    return ""


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


def _number_text(value: Any) -> str:
    text = _safe_text(value)
    if text == NA:
        return NA
    try:
        normalized = format(Decimal(text), "f").rstrip("0").rstrip(".")
    except (ArithmeticError, ValueError):
        return text
    return normalized or "0"

def _safe_text(value: Any) -> str:
    if value is None:
        return NA
    text = str(getattr(value, "value", value)).strip()
    if not text:
        return NA
    return _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}: [REDACTED]", text)


__all__ = ["ConsoleMode", "ScannerConsolePresenter", "format_watch_iteration_console"]
