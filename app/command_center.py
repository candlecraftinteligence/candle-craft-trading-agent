from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from app.analytics.portfolio_selection import PortfolioDecision, PortfolioSelectionResult, selected_symbols
from app.core.minimum_rr import minimum_rr_policy
from app.data.dtos import NA
from app.formatters.scanner_display import (
    RankedSymbolDisplay,
    build_symbol_display,
    rank_scan_results,
    representative_strategy_diagnostics,
)
from app.pipeline.scanner_runner import ScannerRunResult, ScannerSymbolResult


CORRELATION_REJECTION_DECISIONS = {
    PortfolioDecision.REJECTED_LOWER_QUALITY_DUPLICATE.value,
    PortfolioDecision.REJECTED_CORRELATED_EXPOSURE.value,
}


def build_command_center_payload(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    portfolio_selection: PortfolioSelectionResult | None = None,
    promoted_watch_symbols: Sequence[str] = (),
    continued_watch_symbols: Sequence[str] = (),
    command_preset: str | None = None,
    min_rr: Decimal | None = None,
    replay_warnings: Sequence[str] = (),
) -> dict[str, Any]:
    ranked = tuple(ranked_results) if ranked_results is not None else rank_scan_results(result.results)
    valid_items = tuple(item for item in ranked if item.display.display_bucket == "valid")
    near_miss_items = tuple(item for item in ranked if item.display.display_bucket == "near_miss")
    top_setup = _top_ranked_setup(valid_items)
    highest_confidence = _highest_confidence_setup(valid_items)
    runtime = result.runtime_stats
    cache_stats = result.cache_stats or {}
    runtime_warnings = _runtime_warnings(result, replay_warnings)

    configured_minimum_rr = result.config.min_rr
    payload: dict[str, Any] = {
        "title": "DAILY COMMAND CENTER",
        "command_preset": command_preset or NA,
        "configured_min_rr": _display(configured_minimum_rr),
        "minimum_rr_policy": build_minimum_rr_policy_payload(result),
        "minimum_rr_audit": build_minimum_rr_audit(result),
        "total_symbols_scanned": result.scanned_symbols,
        "valid_setups": len(valid_items),
        "near_misses": len(near_miss_items),
        "best_setup": _display(top_setup.symbol_result.symbol if top_setup is not None else NA),
        "average_edge_score": _average_edge_score(result.results),
        "highest_confidence_setup": _display(
            highest_confidence.symbol_result.symbol if highest_confidence is not None else NA
        ),
        "portfolio_selected_names": list(selected_symbols(portfolio_selection)),
        "promoted_watch_symbols": list(_unique_symbols(promoted_watch_symbols)),
        "continued_watch_symbols": list(_unique_symbols(continued_watch_symbols)),
        "most_common_rejection_reason": _most_common_rejection_reason(ranked),
        "scan_runtime": _seconds_text(runtime.total_runtime_seconds),
        "data_quality_status": _data_quality_status(result),
        "runtime_metrics": {
            "total_runtime": _seconds_text(runtime.total_runtime_seconds),
            "average_symbol_runtime": _seconds_text(runtime.average_seconds_per_symbol),
            "slowest_symbol": _slowest_symbol_text(result),
            "retry_count": len(result.retry_diagnostics),
            "cache_efficiency": _cache_efficiency(cache_stats),
            "runtime_warnings": list(runtime_warnings),
            "process_memory": runtime.process_memory.model_dump(mode="json"),
        },
        "performance_memory_summary": _performance_memory_payload(result),
    }
    if top_setup is not None:
        payload["top_setup"] = build_top_setup_payload(top_setup.symbol_result)
    if portfolio_selection is not None:
        payload["portfolio_summary"] = build_portfolio_summary_payload(portfolio_selection)
    return payload


def build_minimum_rr_policy_payload(result: ScannerRunResult) -> dict[str, Any]:
    configured = result.config.min_rr
    modes: dict[str, Any] = {}
    for mode in ("scalp", "swing", "challenge"):
        policy = minimum_rr_policy(configured, mode)
        modes[mode] = {
            "configured_global_minimum_rr": _display(policy.configured_global_minimum_rr),
            "hard_mode_floor": _display(policy.hard_mode_floor),
            "effective_minimum_rr": _display(policy.effective_minimum_rr),
        }
    return {
        "configured_global_minimum_rr": _display(configured),
        "modes": modes,
    }


def build_minimum_rr_audit(result: ScannerRunResult) -> list[dict[str, Any]]:
    audit_rows: list[dict[str, Any]] = []
    for symbol_result in result.results:
        for mode, diagnostics in symbol_result.strategy_diagnostics.items():
            if not isinstance(diagnostics, Mapping):
                continue
            audit_rows.append(
                {
                    "symbol": symbol_result.symbol,
                    "mode": mode,
                    "configured_global_minimum_rr": _display(
                        diagnostics.get("configured_global_minimum_rr")
                    ),
                    "hard_mode_floor": _display(diagnostics.get("hard_mode_floor")),
                    "effective_minimum_rr": _display(diagnostics.get("effective_minimum_rr")),
                    "candidate_rr": _display(
                        _first_non_na(diagnostics.get("candidate_rr"), diagnostics.get("rr_to_tp2"))
                    ),
                    "rr_rejection_reason": _display(diagnostics.get("rr_rejection_reason")),
                }
            )
    return audit_rows


def format_command_center_summary(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    portfolio_selection: PortfolioSelectionResult | None = None,
    promoted_watch_symbols: Sequence[str] = (),
    continued_watch_symbols: Sequence[str] = (),
    command_preset: str | None = None,
    min_rr: Decimal | None = None,
    replay_warnings: Sequence[str] = (),
) -> str:
    payload = build_command_center_payload(
        result,
        ranked_results=ranked_results,
        portfolio_selection=portfolio_selection,
        promoted_watch_symbols=promoted_watch_symbols,
        continued_watch_symbols=continued_watch_symbols,
        command_preset=command_preset,
        min_rr=min_rr,
        replay_warnings=replay_warnings,
    )
    runtime = payload["runtime_metrics"]
    return "\n".join(
        (
            "DAILY COMMAND CENTER",
            f"- Preset: {payload['command_preset']}",
            f"- Total symbols scanned: {payload['total_symbols_scanned']}",
            f"- Valid setups: {payload['valid_setups']}",
            f"- Near misses: {payload['near_misses']}",
            f"- Best setup: {payload['best_setup']}",
            f"- Average edge score: {payload['average_edge_score']}",
            f"- Highest confidence setup: {payload['highest_confidence_setup']}",
            f"- Portfolio selected names: {_sequence_text(payload['portfolio_selected_names'])}",
            f"- Symbols promoted into watch mode: {_sequence_text(payload['promoted_watch_symbols'])}",
            f"- Continued watch symbols: {_sequence_text(payload['continued_watch_symbols'])}",
            f"- Most common rejection reason: {payload['most_common_rejection_reason']}",
            f"- Scan runtime: {payload['scan_runtime']}",
            f"- Data quality status: {payload['data_quality_status']}",
            *_performance_memory_summary_lines(payload["performance_memory_summary"]),
            f"- Runtime metrics: avg symbol {runtime['average_symbol_runtime']}; "
            f"slowest {runtime['slowest_symbol']}; retries {runtime['retry_count']}; "
            f"cache efficiency {runtime['cache_efficiency']}",
            f"- Replay/runtime warnings: {_sequence_text(runtime['runtime_warnings'])}",
        )
    )


def build_top_setup_payload(symbol_result: ScannerSymbolResult) -> dict[str, Any]:
    diagnostics = dict(representative_strategy_diagnostics(symbol_result))
    quality = symbol_result.setup_quality
    trade_idea = symbol_result.trade_idea
    direction = _first_non_na(
        _attr(trade_idea, "direction"),
        diagnostics.get("direction"),
        diagnostics.get("bias"),
        diagnostics.get("setup_direction"),
    )
    edge_score = _first_non_na(
        _attr(quality, "profitability_edge_score"),
        _attr(_attr(symbol_result, "score_result"), "total_score"),
        diagnostics.get("trust_percentage"),
    )
    risk_score = _first_non_na(_attr(quality, "execution_risk_score"), NA)
    rr = _first_non_na(_attr(trade_idea, "best_rr"), _best_rr(symbol_result, diagnostics))
    why = _why_setup_qualifies(symbol_result)
    return {
        "symbol": symbol_result.symbol,
        "direction": _display(direction).upper() if _display(direction) != NA else NA,
        "grade": _display(_enum_value(_attr(quality, "quality_grade"))),
        "edge_score": _display(edge_score),
        "risk_score": _display(risk_score),
        "rr": _display(rr),
        "why_it_qualifies": why,
        "trigger_condition": _trigger_condition(symbol_result),
        "invalidation": _first_non_na(_attr(trade_idea, "invalidation"), diagnostics.get("invalidation")),
        "setup_quality_explanation": _display(_attr(quality, "decision_reason")),
        "risk_warning": _display(_attr(trade_idea, "risk_warning")),
    }


def format_top_setup_spotlight(symbol_result: ScannerSymbolResult | None) -> str:
    if symbol_result is None:
        return ""
    payload = build_top_setup_payload(symbol_result)
    return "\n".join(
        (
            "TOP SETUP",
            f"- Symbol: {payload['symbol']}",
            f"- Direction: {payload['direction']}",
            f"- Grade: {payload['grade']}",
            f"- Edge score: {payload['edge_score']}",
            f"- Risk score: {payload['risk_score']}",
            f"- RR: {payload['rr']}",
            f"- Why it qualifies: {payload['why_it_qualifies']}",
            f"- Trigger condition: {payload['trigger_condition']}",
            f"- Invalidation: {payload['invalidation']}",
            f"- Setup quality explanation: {payload['setup_quality_explanation']}",
            f"- Risk warning: {payload['risk_warning']}",
        )
    )


def build_portfolio_summary_payload(selection: PortfolioSelectionResult) -> dict[str, Any]:
    rejected_correlated = tuple(
        candidate.symbol
        for candidate in selection.rejected_candidates
        if _display(candidate.decision) in CORRELATION_REJECTION_DECISIONS
    )
    strongest = selection.selected_candidates[0] if selection.selected_candidates else None
    return {
        "total_selected_setups": selection.selected_count,
        "total_combined_risk": f"{_display(selection.total_risk_pct)}%",
        "correlation_groups": _correlation_groups_text(selection),
        "rejected_correlated_symbols": list(rejected_correlated),
        "strongest_portfolio_candidate": strongest.symbol if strongest is not None else NA,
        "warnings": list(selection.portfolio_warnings),
    }


def _performance_memory_payload(result: ScannerRunResult) -> dict[str, Any]:
    summary = result.performance_memory_summary
    if not isinstance(summary, Mapping) or not summary:
        return {
            "best_performing_setup_type": NA,
            "weakest_setup_type": NA,
            "best_regime_historically": NA,
            "worst_regime_historically": NA,
            "strongest_symbols": NA,
            "weakest_symbols": NA,
            "memory_confidence_level": "VERY_LOW",
            "total_historical_samples": 0,
        }
    return dict(summary)


def _performance_memory_summary_lines(summary: Mapping[str, Any]) -> tuple[str, ...]:
    if summary.get("enabled") is False:
        return ("- Performance Memory Summary: disabled",)
    return (
        "- Performance Memory Summary:",
        f"  Best-performing setup type: {_display(summary.get('best_performing_setup_type'))}",
        f"  Weakest setup type: {_display(summary.get('weakest_setup_type'))}",
        f"  Best regime historically: {_display(summary.get('best_regime_historically'))}",
        f"  Worst regime historically: {_display(summary.get('worst_regime_historically'))}",
        f"  Strongest symbols: {_display(summary.get('strongest_symbols'))}",
        f"  Weakest symbols: {_display(summary.get('weakest_symbols'))}",
        f"  Memory confidence level: {_display(summary.get('memory_confidence_level'))}",
        f"  Total historical samples: {_display(summary.get('total_historical_samples'))}",
    )


def format_portfolio_command_summary(selection: PortfolioSelectionResult | None) -> str:
    if selection is None:
        return ""
    payload = build_portfolio_summary_payload(selection)
    return "\n".join(
        (
            "PORTFOLIO SUMMARY",
            f"- Total selected setups: {payload['total_selected_setups']}",
            f"- Total combined risk: {payload['total_combined_risk']}",
            f"- Correlation groups: {payload['correlation_groups']}",
            f"- Rejected correlated symbols: {_sequence_text(payload['rejected_correlated_symbols'])}",
            f"- Strongest portfolio candidate: {payload['strongest_portfolio_candidate']}",
            f"- Portfolio warnings: {_sequence_text(payload['warnings'])}",
        )
    )


def format_command_center_report(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    portfolio_selection: PortfolioSelectionResult | None = None,
    promoted_watch_symbols: Sequence[str] = (),
    continued_watch_symbols: Sequence[str] = (),
    command_preset: str | None = None,
    min_rr: Decimal | None = None,
    replay_warnings: Sequence[str] = (),
) -> str:
    ranked = tuple(ranked_results) if ranked_results is not None else rank_scan_results(result.results)
    top = _top_ranked_setup(tuple(item for item in ranked if item.display.display_bucket == "valid"))
    sections = [
        format_command_center_summary(
            result,
            ranked_results=ranked,
            portfolio_selection=portfolio_selection,
            promoted_watch_symbols=promoted_watch_symbols,
            continued_watch_symbols=continued_watch_symbols,
            command_preset=command_preset,
            min_rr=min_rr,
            replay_warnings=replay_warnings,
        )
    ]
    if top is not None:
        sections.append(format_top_setup_spotlight(top.symbol_result))
    if portfolio_selection is not None:
        sections.append(format_portfolio_command_summary(portfolio_selection))
    sections.append(_compact_result_lines(ranked))
    return "\n\n".join(section for section in sections if section)


def format_watchlist_export(
    result: ScannerRunResult,
    *,
    promoted_watch_symbols: Sequence[str] = (),
    continued_watch_symbols: Sequence[str] = (),
) -> str:
    promoted = _unique_symbols(promoted_watch_symbols)
    continued = _unique_symbols(continued_watch_symbols)
    lines = [
        "WATCHLIST SUMMARY",
        f"- Continued symbols: {_sequence_text(continued)}",
        f"- Promoted symbols: {_sequence_text(promoted)}",
        "",
        "Tracked candidates:",
    ]
    candidate_symbols = set(promoted) | set(continued)
    if not candidate_symbols:
        lines.append("- N/A")
        return "\n".join(lines)

    for symbol_result in result.results:
        if symbol_result.symbol not in candidate_symbols:
            continue
        display = build_symbol_display(symbol_result)
        intelligence = display.near_miss_intelligence
        activation = intelligence.activation_hint if intelligence is not None else display.next_trigger_needed
        invalidation = intelligence.invalidation_hint if intelligence is not None else NA
        lines.append(
            f"- {symbol_result.symbol}: {display.readiness_label}; trigger {activation}; invalidation {invalidation}"
        )
    return "\n".join(lines)


def _top_ranked_setup(ranked_results: Sequence[RankedSymbolDisplay]) -> RankedSymbolDisplay | None:
    return ranked_results[0] if ranked_results else None


def _highest_confidence_setup(ranked_results: Sequence[RankedSymbolDisplay]) -> RankedSymbolDisplay | None:
    if not ranked_results:
        return None
    return max(
        ranked_results,
        key=lambda item: (
            _numeric(_attr(item.symbol_result.trade_idea, "confidence_score")),
            _numeric(_attr(item.symbol_result.setup_quality, "quality_score")),
            _numeric(_attr(item.symbol_result.setup_quality, "profitability_edge_score")),
        ),
    )


def _average_edge_score(results: Sequence[ScannerSymbolResult]) -> str:
    scores = [
        _numeric(_attr(result.setup_quality, "profitability_edge_score"))
        for result in results
        if _attr(result.setup_quality, "is_evaluated") is True
    ]
    scores = [score for score in scores if score > 0]
    if not scores:
        return NA
    average = sum(scores, Decimal("0")) / Decimal(len(scores))
    return _decimal_text(average.quantize(Decimal("0.1")))


def _most_common_rejection_reason(ranked_results: Sequence[RankedSymbolDisplay]) -> str:
    reasons = [
        item.display.short_reason
        for item in ranked_results
        if item.display.display_bucket != "valid" and _display(item.display.short_reason) != NA
    ]
    if not reasons:
        return NA
    reason, count = Counter(reasons).most_common(1)[0]
    return f"{reason} ({count})" if count > 1 else reason


def _data_quality_status(result: ScannerRunResult) -> str:
    has_data_issue = False
    has_unverified = False
    for symbol_result in result.results:
        display = build_symbol_display(symbol_result)
        if display.display_bucket == "data_issue" or symbol_result.error_message:
            has_data_issue = True
        if (
            symbol_result.unverified_data
            or symbol_result.strategy_unverified_data
            or symbol_result.derivatives_unverified_data
            or symbol_result.derivatives_warnings
        ):
            has_unverified = True
        if symbol_result.missing_data or symbol_result.strategy_missing_data or symbol_result.derivatives_missing_data:
            has_unverified = True
    if has_data_issue:
        return "N/A"
    if has_unverified:
        return "Unverified"
    return "Verified"


def _runtime_warnings(result: ScannerRunResult, replay_warnings: Sequence[str]) -> tuple[str, ...]:
    runtime = result.runtime_stats
    warnings: list[str] = list(replay_warnings)
    if runtime.global_timeout_hit:
        warnings.append("global scan timeout hit")
    if runtime.timeout_count:
        warnings.append(f"{runtime.timeout_count} symbol timeout(s)")
    if runtime.skipped_symbols:
        warnings.append(f"{runtime.skipped_symbols} skipped symbol(s)")
    if result.retry_diagnostics:
        warnings.append(f"{len(result.retry_diagnostics)} retry event(s)")
    return tuple(warnings)


def _cache_efficiency(cache_stats: Mapping[str, Any]) -> str:
    hits = _int_value(cache_stats.get("hits", 0))
    misses = _int_value(cache_stats.get("misses", 0))
    total = hits + misses
    if total <= 0:
        return NA
    pct = Decimal(hits) / Decimal(total) * Decimal("100")
    return f"{_decimal_text(pct.quantize(Decimal('0.1')))}% ({hits}/{total})"


def _slowest_symbol_text(result: ScannerRunResult) -> str:
    runtime = result.runtime_stats
    if _display(runtime.slowest_symbol) == NA:
        return NA
    return f"{runtime.slowest_symbol} ({_seconds_text(runtime.slowest_symbol_seconds)})"


def _why_setup_qualifies(symbol_result: ScannerSymbolResult) -> str:
    quality = symbol_result.setup_quality
    raw_factors = _attr(quality, "strongest_factors")
    factors = tuple(raw_factors) if isinstance(raw_factors, Sequence) and not isinstance(raw_factors, str) else ()
    decision_reason = _display(_attr(quality, "decision_reason"))
    if factors:
        return f"{', '.join(str(item) for item in factors)}. {decision_reason}"
    return decision_reason


def _trigger_condition(symbol_result: ScannerSymbolResult) -> str:
    trade_idea = symbol_result.trade_idea
    entry_zone = _level_text(_attr(trade_idea, "entry_zone"))
    status = _display(_attr(trade_idea, "status"))
    if entry_zone != NA and status != NA:
        return f"{status}: entry zone {entry_zone}"
    if entry_zone != NA:
        return f"Entry zone {entry_zone}"
    diagnostics = representative_strategy_diagnostics(symbol_result)
    return _first_non_na(diagnostics.get("entry_trigger"), diagnostics.get("trigger_condition"))


def _level_text(level: Any) -> str:
    price = _display(_attr(level, "price"))
    low = _display(_attr(level, "low"))
    high = _display(_attr(level, "high"))
    if price != NA:
        return price
    if low != NA and high != NA:
        return low if low == high else f"{low} - {high}"
    if low != NA:
        return low
    if high != NA:
        return high
    return NA


def _best_rr(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    values = (
        _attr(_attr(symbol_result, "risk_decision"), "best_risk_reward_ratio"),
        _attr(_attr(_attr(symbol_result, "score_result"), "score_breakdown"), "best_rr"),
        diagnostics.get("rr_to_tp2"),
        diagnostics.get("best_rr"),
    )
    decimals = [_numeric(value) for value in values]
    numeric = [value for value in decimals if value > 0]
    if not numeric:
        return NA
    return _decimal_text(max(numeric))


def _correlation_groups_text(selection: PortfolioSelectionResult) -> str:
    if not selection.exposure_summary:
        return NA
    return ", ".join(
        f"{item.beta_group.value} {', '.join(item.symbols)} risk {_display(item.risk_pct)}%"
        for item in selection.exposure_summary
    )


def _compact_result_lines(ranked_results: Sequence[RankedSymbolDisplay]) -> str:
    if not ranked_results:
        return "RANKED RESULTS\n- N/A"
    lines = ["RANKED RESULTS"]
    for item in ranked_results[:10]:
        lines.append(
            f"- #{item.display_rank} {item.symbol_result.symbol}: "
            f"{item.display.display_bucket_label}; {item.display.short_reason}"
        )
    return "\n".join(lines)


def _unique_symbols(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        symbol = str(value).strip().upper()
        if symbol and symbol not in output:
            output.append(symbol)
    return tuple(output)


def _sequence_text(values: Sequence[Any]) -> str:
    strings = tuple(_display(value) for value in values if _display(value) != NA)
    return ", ".join(strings) if strings else NA


def _first_non_na(*values: Any) -> str:
    for value in values:
        text = _display(value)
        if text != NA:
            return text
    return NA


def _attr(source: Any, name: str | None = None) -> Any:
    if source is None:
        return NA
    if name is None:
        return source
    if isinstance(source, Mapping):
        return source.get(name, NA)
    return getattr(source, name, NA)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _numeric(value: Any) -> Decimal:
    text = _display(_enum_value(value))
    if text == NA:
        return Decimal("0")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _seconds_text(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    try:
        seconds = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if seconds == 0:
        return "0s"
    if seconds < Decimal("1"):
        return f"{seconds:.3f}".rstrip("0").rstrip(".") + "s"
    return f"{seconds:.1f}".rstrip("0").rstrip(".") + "s"


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _display(value: Any) -> str:
    value = _enum_value(value)
    if value is None or value == "" or value == NA:
        return NA
    if isinstance(value, Decimal):
        return _decimal_text(value)
    return str(value)


__all__ = [
    "build_command_center_payload",
    "build_portfolio_summary_payload",
    "build_top_setup_payload",
    "format_command_center_report",
    "format_command_center_summary",
    "format_portfolio_command_summary",
    "format_top_setup_spotlight",
    "format_watchlist_export",
]
