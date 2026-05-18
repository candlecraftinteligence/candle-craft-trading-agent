from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.data.dtos import NA
from app.research.queries import SAMPLE_SIZE_WARNING


def format_research_report(report: Mapping[str, Any]) -> str:
    query = str(report.get("query", NA))
    if report.get("error"):
        return "\n".join(("Candle Craft Research", str(report["error"])))
    if query == "summary":
        return _format_summary(report)
    if query in {"best_symbols", "worst_symbols"}:
        return _format_symbols(report)
    if query in {"best_regimes", "worst_regimes"}:
        return _format_regimes(report)
    if query == "rejection_reasons":
        return _format_rejection_reasons(report)
    if query == "setup_quality":
        return _format_setup_quality(report)
    if query == "near_misses":
        return _format_near_misses(report)
    if query == "replay_expectancy":
        return _format_replay_expectancy(report)
    if query == "mode_performance":
        return _format_mode_performance(report)
    if query == "symbol_detail":
        return _format_symbol_detail(report)
    return "\n".join(("Candle Craft Research", f"Unsupported report: {query}"))


def _format_summary(report: Mapping[str, Any]) -> str:
    summary = _mapping(report.get("summary"))
    rows = (
        ("Total scan runs", summary.get("total_scan_runs")),
        ("Total symbols scanned", summary.get("total_symbols_scanned")),
        ("Total valid setups", summary.get("total_valid_setups")),
        ("Total near misses", summary.get("total_near_misses")),
        ("Total rejected", summary.get("total_rejected")),
        ("Total replay outcomes", summary.get("total_replay_outcomes")),
        ("Average readiness score", summary.get("average_readiness_score")),
        ("Average quality score", summary.get("average_quality_score")),
        ("Most common regime", summary.get("most_common_regime")),
        ("Most common rejection reason", summary.get("most_common_rejection_reason")),
    )
    return _join_sections(
        "Candle Craft Research - Summary",
        _metric_table(rows),
        _warning_block(report),
    )


def _format_symbols(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("symbol"),
            row.get("scan_count"),
            row.get("valid_setups"),
            row.get("near_misses"),
            row.get("average_quality_score"),
            _r_value(row.get("replay_expectancy_r")),
            row.get("near_miss_quality_score"),
            _pct_value(row.get("data_completeness_pct")),
        )
        for row in _sequence(report.get("symbols"))
    ]
    return _join_sections(
        str(report.get("title", "Symbols")),
        _table(
            (
                "symbol",
                "scans",
                "valid",
                "near",
                "avg_quality",
                "expectancy",
                "near_quality",
                "data_ok",
            ),
            rows,
        ),
        _warning_block(report),
    )


def _format_regimes(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("regime"),
            row.get("setups"),
            row.get("valid_setups"),
            row.get("near_misses"),
            row.get("average_quality_score"),
            _r_value(row.get("replay_expectancy_r")),
            _pct_value(row.get("tp1_rate_pct")),
            _pct_value(row.get("tp2_rate_pct")),
        )
        for row in _sequence(report.get("regimes"))
    ]
    return _join_sections(
        str(report.get("title", "Regimes")),
        _table(
            ("regime", "setups", "valid", "near", "avg_quality", "expectancy", "TP1", "TP2"),
            rows,
        ),
        _warning_block(report),
    )


def _format_rejection_reasons(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("failed_gate"),
            row.get("count"),
            _pct_value(row.get("percentage")),
            row.get("affected_symbol_count"),
            row.get("possible_interpretation"),
        )
        for row in _sequence(report.get("reasons"))
    ]
    return _join_sections(
        "Rejection Reasons",
        f"Total rejected/near/data issue rows: {_display(report.get('total_rejections'))}",
        _table(("failed_gate", "count", "pct", "symbols", "interpretation"), rows),
        _warning_block(report),
    )


def _format_setup_quality(report: Mapping[str, Any]) -> str:
    grade_rows = [
        (
            row.get("quality_grade"),
            row.get("count"),
            row.get("valid_setup_count"),
            _pct_value(row.get("conversion_to_valid_pct")),
            row.get("average_quality_score"),
        )
        for row in _sequence(report.get("quality_grades"))
    ]
    readiness_rows = [
        (
            row.get("readiness_label"),
            row.get("count"),
            row.get("valid_setup_count"),
            _pct_value(row.get("conversion_to_valid_pct")),
            row.get("average_quality_score"),
        )
        for row in _sequence(report.get("readiness_labels"))
    ]
    state_rows = [
        (
            row.get("quality_state"),
            row.get("count"),
            row.get("valid_setup_count"),
            _pct_value(row.get("conversion_to_valid_pct")),
            row.get("average_quality_score"),
        )
        for row in _sequence(report.get("quality_states"))
    ]
    return _join_sections(
        "Setup Quality",
        _metric_table(
            (
                ("Total symbols", report.get("total_symbols")),
                ("Average quality score", report.get("average_quality_score")),
            )
        ),
        "Quality grades",
        _table(("grade", "count", "valid", "valid_pct", "avg_quality"), grade_rows),
        "Readiness labels",
        _table(("label", "count", "valid", "valid_pct", "avg_quality"), readiness_rows),
        "Quality states",
        _table(("state", "count", "valid", "valid_pct", "avg_quality"), state_rows),
        _warning_block(report),
    )


def _format_near_misses(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("symbol"),
            row.get("near_miss_count"),
            row.get("most_common_failed_gate"),
            row.get("most_common_next_trigger_needed"),
            row.get("average_quality_score"),
            row.get("later_became_valid"),
        )
        for row in _sequence(report.get("symbols"))
    ]
    return _join_sections(
        "Near Misses",
        _metric_table(
            (
                ("Total near misses", report.get("total_near_misses")),
                ("Most common failed gate", report.get("most_common_failed_gate")),
                ("Most common next trigger", report.get("most_common_next_trigger_needed")),
            )
        ),
        _table(("symbol", "count", "failed_gate", "next_trigger", "avg_quality", "later_valid"), rows),
        _warning_block(report),
    )


def _format_replay_expectancy(report: Mapping[str, Any]) -> str:
    replay = _mapping(report.get("replay"))
    rows = (
        ("Total replay samples", replay.get("total_replay_samples")),
        ("Filled samples", replay.get("filled_samples")),
        ("Avg R", _r_value(replay.get("avg_r"))),
        ("Median R", _r_value(replay.get("median_r"))),
        ("Win rate", _pct_value(replay.get("win_rate_pct"))),
        ("TP1 rate", _pct_value(replay.get("tp1_rate_pct"))),
        ("TP2 rate", _pct_value(replay.get("tp2_rate_pct"))),
        ("Expectancy", _r_value(replay.get("expectancy_r"))),
    )
    return _join_sections(
        "Replay Expectancy",
        _metric_table(rows),
        _warning_block(report),
    )


def _format_mode_performance(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("mode"),
            row.get("symbols_scanned"),
            row.get("setup_candidates"),
            row.get("valid_setups"),
            row.get("near_misses"),
            row.get("average_quality_score"),
            _r_value(row.get("expectancy_r")),
            _pct_value(row.get("tp1_rate_pct")),
            _pct_value(row.get("tp2_rate_pct")),
        )
        for row in _sequence(report.get("modes"))
    ]
    return _join_sections(
        "Mode Performance",
        _table(
            ("mode", "symbols", "candidates", "valid", "near", "avg_quality", "expectancy", "TP1", "TP2"),
            rows,
        ),
        _warning_block(report),
    )


def _format_symbol_detail(report: Mapping[str, Any]) -> str:
    if report.get("error"):
        return "\n".join(("Symbol Detail", str(report["error"])))
    replay = _mapping(report.get("replay"))
    recent_rows = [
        (
            row.get("timestamp"),
            row.get("display_bucket"),
            row.get("failed_gate"),
            row.get("quality_score"),
            row.get("readiness_score"),
            row.get("next_trigger_needed"),
        )
        for row in _sequence(report.get("recent_history"))
    ]
    return _join_sections(
        f"Symbol Detail - {_display(report.get('symbol'))}",
        _metric_table(
            (
                ("Scan count", report.get("scan_count")),
                ("Valid setup count", report.get("valid_setup_count")),
                ("Near miss count", report.get("near_miss_count")),
                ("Most common failed gate", report.get("most_common_failed_gate")),
                ("Average quality score", report.get("average_quality_score")),
                ("Replay samples", replay.get("total_replay_samples")),
                ("Replay expectancy", _r_value(replay.get("expectancy_r"))),
                ("TP1 rate", _pct_value(replay.get("tp1_rate_pct"))),
                ("TP2 rate", _pct_value(replay.get("tp2_rate_pct"))),
            )
        ),
        "Recent history",
        _table(("timestamp", "bucket", "failed_gate", "quality", "ready", "next_trigger"), recent_rows),
        _warning_block(report),
    )


def _warning_block(report: Mapping[str, Any]) -> str:
    warnings = [str(warning) for warning in _sequence(report.get("warnings"))]
    if not warnings:
        return ""
    lines = ["Warnings"]
    for warning in warnings:
        if warning == SAMPLE_SIZE_WARNING:
            lines.append(SAMPLE_SIZE_WARNING)
        else:
            lines.append(warning)
    return "\n".join(lines)


def _metric_table(rows: Sequence[tuple[Any, Any]]) -> str:
    return _table(("metric", "value"), rows)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return "N/A"
    text_rows = [tuple(_display(value) for value in row) for row in rows]
    widths = [
        max(len(str(headers[index])), *(len(row[index]) for row in text_rows))
        for index in range(len(headers))
    ]
    lines = [
        " | ".join(str(headers[index]).ljust(widths[index]) for index in range(len(headers))),
        "-+-".join("-" * width for width in widths),
    ]
    lines.extend(
        " | ".join(row[index].ljust(widths[index]) for index in range(len(headers)))
        for row in text_rows
    )
    return "\n".join(lines)


def _join_sections(*sections: str) -> str:
    return "\n\n".join(section for section in sections if section)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _pct_value(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    return f"{text}%"


def _r_value(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    return f"{text}R"


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
