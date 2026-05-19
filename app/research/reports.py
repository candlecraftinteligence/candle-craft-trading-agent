from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.data.dtos import NA
from app.research.queries import SAMPLE_SIZE_WARNING

LIFECYCLE_FUNNEL_DISPLAY_STATES = (
    "DISCOVERED",
    "WATCHLISTED",
    "STALKING",
    "TRIGGERED",
    "CONFIRMED",
    "EXECUTING",
    "TP_HIT",
    "SL_HIT",
    "INVALIDATED",
    "ARCHIVED",
)


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
    if query == "regime_expectancy":
        return _format_regime_expectancy(report)
    if query == "regime_setup_density":
        return _format_regime_setup_density(report)
    if query == "regime_rejection_patterns":
        return _format_regime_rejection_patterns(report)
    if query == "regime_quality_distribution":
        return _format_regime_quality_distribution(report)
    if query == "lifecycle_summary":
        return _format_lifecycle_summary(report)
    if query == "lifecycle_transitions":
        return _format_lifecycle_transitions(report)
    if query == "lifecycle_conversion":
        return _format_lifecycle_conversion(report)
    if query == "lifecycle_funnel":
        return _format_lifecycle_funnel(report)
    if query == "lifecycle_dropoffs":
        return _format_lifecycle_dropoffs(report)
    if query == "lifecycle_symbol_conversion":
        return _format_lifecycle_symbol_conversion(report)
    if query == "lifecycle_state_duration":
        return _format_lifecycle_state_duration(report)
    if query == "lifecycle_symbol_detail":
        return _format_lifecycle_symbol_detail(report)
    if query == "pullback_failures":
        return _format_pullback_failures(report)
    if query == "pullback_quality_distribution":
        return _format_pullback_quality_distribution(report)
    if query == "pullback_depth_analysis":
        return _format_pullback_depth_analysis(report)
    if query == "pullback_lifecycle_dropoffs":
        return _format_pullback_lifecycle_dropoffs(report)
    if query in {"symbol_health", "slow_symbols", "timeout_symbols", "priority_symbols"}:
        return _format_symbol_health(report)
    if query == "watch_iterations":
        return _format_watch_iterations(report)
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
        ("Total watch iterations", summary.get("total_watch_iterations")),
        ("Last watch iteration", summary.get("last_watch_iteration")),
        ("Average symbols/watch iteration", summary.get("average_symbols_per_watch_iteration")),
        ("Valid activations from watch", summary.get("valid_activations_from_watch")),
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


def _format_watch_iterations(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("iteration_number"),
            row.get("timestamp"),
            row.get("symbols_watched"),
            row.get("valid_activations"),
            row.get("still_watching"),
            row.get("data_issues"),
            row.get("runtime"),
            row.get("regime"),
        )
        for row in _sequence(report.get("watch_iterations"))
    ]
    return _join_sections(
        str(report.get("title", "Watch Iterations")),
        f"Total watch iterations: {_display(report.get('total_watch_iterations'))}",
        _table(("iteration", "timestamp", "symbols", "valid", "still", "data", "runtime", "regime"), rows),
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


def _format_symbol_health(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("symbol"),
            row.get("current_health_score"),
            row.get("successful_scans"),
            row.get("timeout_count"),
            row.get("timeout_strikes"),
            row.get("data_issue_count"),
            row.get("average_runtime_sec"),
            row.get("last_priority_rank"),
            row.get("cooldown_until"),
        )
        for row in _sequence(report.get("symbols"))
    ]
    return _join_sections(
        str(report.get("title", "Symbol Health")),
        _table(
            (
                "symbol",
                "health",
                "success",
                "timeouts",
                "strikes",
                "data",
                "avg_sec",
                "rank",
                "cooldown_until",
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


def _format_regime_expectancy(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("regime"),
            row.get("average_confidence"),
            row.get("symbols_scanned"),
            row.get("valid_setups"),
            row.get("replay_samples"),
            _r_value(row.get("expectancy_r")),
            _pct_value(row.get("win_rate_pct")),
            _pct_value(row.get("tp1_rate_pct")),
            _pct_value(row.get("tp2_rate_pct")),
        )
        for row in _sequence(report.get("regimes"))
    ]
    return _join_sections(
        "Regime Expectancy",
        _table(("regime", "conf", "symbols", "valid", "samples", "expectancy", "win", "TP1", "TP2"), rows),
        _warning_block(report),
    )


def _format_regime_setup_density(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("regime"),
            row.get("symbols_scanned"),
            row.get("setup_candidates"),
            row.get("valid_setups"),
            row.get("near_misses"),
            row.get("rejected"),
            _pct_value(row.get("setup_density_pct")),
            _pct_value(row.get("valid_density_pct")),
        )
        for row in _sequence(report.get("regimes"))
    ]
    return _join_sections(
        "Regime Setup Density",
        _table(("regime", "symbols", "candidates", "valid", "near", "rejected", "setup_density", "valid_density"), rows),
        _warning_block(report),
    )


def _format_regime_rejection_patterns(report: Mapping[str, Any]) -> str:
    lines = ["Regime Rejection Patterns"]
    for regime in _sequence(report.get("regimes")):
        lines.append(f"{_display(regime.get('regime'))}: {_display(regime.get('total_rejections'))} rejections")
        pattern_rows = [
            (
                row.get("failed_gate"),
                row.get("count"),
                _pct_value(row.get("percentage")),
                ", ".join(str(symbol) for symbol in _sequence(row.get("affected_symbols"))),
            )
            for row in _sequence(regime.get("patterns"))
        ]
        lines.append(_table(("failed_gate", "count", "pct", "symbols"), pattern_rows))
    return _join_sections("\n\n".join(lines), _warning_block(report))


def _format_regime_quality_distribution(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("regime"),
            row.get("symbols_scanned"),
            row.get("average_quality_score"),
            row.get("average_readiness_score"),
            _top_group(row.get("quality_grades"), "quality_grade"),
            _top_group(row.get("quality_states"), "quality_state"),
            _top_group(row.get("compatibility_labels"), "regime_compatibility_label"),
        )
        for row in _sequence(report.get("regimes"))
    ]
    return _join_sections(
        "Regime Quality Distribution",
        _table(("regime", "symbols", "avg_quality", "avg_ready", "top_grade", "top_state", "top_compat"), rows),
        _warning_block(report),
    )


def _format_lifecycle_summary(report: Mapping[str, Any]) -> str:
    state_rows = [
        (row.get("state"), row.get("count"), _pct_value(row.get("percentage")))
        for row in _sequence(report.get("states"))
    ]
    time_rows = [
        (row.get("state"), row.get("average_seconds"), row.get("samples"))
        for row in _sequence(report.get("average_time_in_state_seconds"))
    ]
    return _join_sections(
        "Lifecycle Summary",
        _metric_table(
            (
                ("Total lifecycles", report.get("total_lifecycles")),
                ("Active lifecycles", report.get("active_lifecycles")),
                ("Most common invalidation", report.get("most_common_invalidation_reason")),
            )
        ),
        "Current states",
        _table(("state", "count", "pct"), state_rows),
        "Average time in state",
        _table(("state", "avg_seconds", "samples"), time_rows),
        _warning_block(report),
    )


def _format_lifecycle_transitions(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("timestamp"),
            row.get("symbol"),
            row.get("from_state"),
            row.get("to_state"),
            row.get("readiness_score"),
            row.get("quality_score"),
            row.get("failed_gate"),
            row.get("reason"),
        )
        for row in _sequence(report.get("transitions"))
    ]
    return _join_sections(
        "Lifecycle Transitions",
        f"Total transitions: {_display(report.get('total_transitions'))}",
        _table(("timestamp", "symbol", "from", "to", "ready", "quality", "gate", "reason"), rows),
        _warning_block(report),
    )


def _format_lifecycle_conversion(report: Mapping[str, Any]) -> str:
    rates = _mapping(report.get("conversion_rates"))
    counts = _mapping(report.get("conversion_counts"))
    executing_tp = _mapping(counts.get("EXECUTING_to_TP_HIT"))
    executing_sl = _mapping(counts.get("EXECUTING_to_SL_HIT"))
    executing_invalidated = _mapping(counts.get("EXECUTING_to_INVALIDATED"))
    executing_expired = _mapping(counts.get("EXECUTING_to_EXPIRED"))
    return _join_sections(
        "Lifecycle Conversion",
        _metric_table(
            (
                ("Total lifecycles", report.get("total_lifecycles")),
                ("Active lifecycles", report.get("active_lifecycles")),
                ("Archived lifecycles", report.get("archived_lifecycles")),
                ("WATCHLISTED -> STALKING", _pct_value(rates.get("watchlisted_to_stalking_pct"))),
                ("STALKING -> TRIGGERED", _pct_value(rates.get("stalking_to_triggered_pct"))),
                ("TRIGGERED -> CONFIRMED", _pct_value(rates.get("triggered_to_confirmed_pct"))),
                ("CONFIRMED -> EXECUTING", _pct_value(rates.get("confirmed_to_executing_pct"))),
                ("EXECUTING -> TP_HIT", _pct_value(executing_tp.get("conversion_rate_pct"))),
                ("EXECUTING -> SL_HIT", _pct_value(executing_sl.get("conversion_rate_pct"))),
                ("EXECUTING -> INVALIDATED", _pct_value(executing_invalidated.get("conversion_rate_pct"))),
                ("EXECUTING -> EXPIRED", _pct_value(executing_expired.get("conversion_rate_pct"))),
            )
        ),
        _warning_block(report),
    )


def _format_lifecycle_funnel(report: Mapping[str, Any]) -> str:
    counts = _mapping(report.get("funnel_counts"))
    rates = _mapping(report.get("conversion_rates"))
    funnel_lines = [
        "Lifecycle Funnel",
        *(f"{state}: {_display(counts.get(state, 0))}" for state in LIFECYCLE_FUNNEL_DISPLAY_STATES),
    ]
    conversion_lines = (
        "Conversion:",
        f"STALKING -> TRIGGERED: {_pct_value(rates.get('stalking_to_triggered_pct'))}",
        f"TRIGGERED -> CONFIRMED: {_pct_value(rates.get('triggered_to_confirmed_pct'))}",
        f"CONFIRMED -> EXECUTING: {_pct_value(rates.get('confirmed_to_executing_pct'))}",
    )
    return _join_sections(
        "\n".join(funnel_lines),
        "\n".join(conversion_lines),
        _warning_block(report),
    )


def _format_lifecycle_dropoffs(report: Mapping[str, Any]) -> str:
    stats = _mapping(report.get("dropoff_stats"))
    stage_rows = [
        (row.get("stage"), row.get("count"))
        for row in _sequence(stats.get("dropoff_stages"))
    ]
    gate_rows = [
        (row.get("failed_gate"), row.get("count"))
        for row in _sequence(stats.get("failed_gate_counts"))
    ]
    return _join_sections(
        "Lifecycle Dropoffs",
        _metric_table(
            (
                ("Dropoff lifecycles", stats.get("dropoff_lifecycle_count")),
                ("Biggest dropoff stage", stats.get("biggest_dropoff_stage")),
                ("Most common failed gate", stats.get("most_common_failed_gate")),
                ("Most common invalidation", stats.get("most_common_invalidation_reason")),
                ("Average readiness at dropoff", stats.get("average_readiness_score")),
                ("Average quality at dropoff", stats.get("average_quality_score")),
                ("Regime at dropoff", stats.get("most_common_regime_state")),
            )
        ),
        "Dropoff stages",
        _table(("stage", "count"), stage_rows),
        "Failed gates",
        _table(("failed_gate", "count"), gate_rows),
        _warning_block(report),
    )


def _format_lifecycle_symbol_conversion(report: Mapping[str, Any]) -> str:
    rows = [
        (
            row.get("symbol"),
            row.get("lifecycle_count"),
            row.get("highest_state_reached"),
            _pct_value(row.get("conversion_to_confirmed_pct")),
            _pct_value(row.get("conversion_to_executing_pct")),
            row.get("average_time_to_highest_state_seconds"),
            row.get("most_common_failure_point"),
        )
        for row in _sequence(report.get("per_symbol_conversion"))
    ]
    return _join_sections(
        "Lifecycle Symbol Conversion",
        _table(("symbol", "lifecycles", "highest", "confirmed", "executing", "avg_to_high", "failure"), rows),
        _warning_block(report),
    )


def _format_lifecycle_state_duration(report: Mapping[str, Any]) -> str:
    stats = _mapping(report.get("state_duration_stats"))
    duration_rows = [
        (
            row.get("state"),
            row.get("average_seconds"),
            row.get("median_seconds"),
            row.get("longest_seconds"),
            row.get("samples"),
        )
        for row in _sequence(stats.get("states"))
    ]
    stuck_rows = [
        (
            row.get("symbol"),
            row.get("current_state"),
            row.get("hours_in_state"),
            row.get("last_transition_at"),
        )
        for row in _sequence(stats.get("longest_stuck_symbols"))
    ]
    return _join_sections(
        "Lifecycle State Duration",
        _metric_table(
            (
                ("Analysis as of", stats.get("analysis_as_of")),
                ("Stale after hours", stats.get("stale_after_hours")),
                ("Stale lifecycle count", stats.get("stale_lifecycle_count")),
            )
        ),
        "State duration",
        _table(("state", "avg_seconds", "median_seconds", "longest_seconds", "samples"), duration_rows),
        "Longest stuck symbols",
        _table(("symbol", "state", "hours", "last_transition"), stuck_rows),
        _warning_block(report),
    )


def _format_lifecycle_symbol_detail(report: Mapping[str, Any]) -> str:
    if report.get("error"):
        return "\n".join(("Lifecycle Symbol Detail", str(report["error"])))
    lifecycle_rows = [
        (
            row.get("mode"),
            row.get("direction"),
            row.get("current_state"),
            row.get("previous_state"),
            row.get("readiness_score"),
            row.get("quality_score"),
            row.get("failed_gate"),
        )
        for row in _sequence(report.get("lifecycles"))
    ]
    transition_rows = [
        (
            row.get("timestamp"),
            row.get("from_state"),
            row.get("to_state"),
            row.get("failed_gate"),
            row.get("reason"),
        )
        for row in _sequence(report.get("recent_transitions"))
    ]
    return _join_sections(
        f"Lifecycle Symbol Detail - {_display(report.get('symbol'))}",
        "Lifecycles",
        _table(("mode", "direction", "state", "previous", "ready", "quality", "gate"), lifecycle_rows),
        "Recent transitions",
        _table(("timestamp", "from", "to", "gate", "reason"), transition_rows),
        _warning_block(report),
    )


def _format_pullback_failures(report: Mapping[str, Any]) -> str:
    failure_rows = [
        (
            row.get("pullback_failure_type"),
            row.get("count"),
            _pct_value(row.get("percentage")),
            row.get("average_depth"),
            ", ".join(str(symbol) for symbol in _sequence(row.get("affected_symbols"))),
        )
        for row in _sequence(report.get("failure_type_counts"))
    ]
    symbol_rows = [
        (
            row.get("symbol"),
            row.get("count"),
            row.get("most_common_failure_type"),
            row.get("average_depth"),
        )
        for row in _sequence(report.get("most_common_failed_symbols"))
    ]
    return _join_sections(
        "Pullback Failures",
        _metric_table((("Total pullback failures", report.get("total_pullback_failures")),)),
        "Failure type counts",
        _table(("failure_type", "count", "pct", "avg_depth", "symbols"), failure_rows),
        "Most common failed symbols",
        _table(("symbol", "count", "top_failure", "avg_depth"), symbol_rows),
        "Failure by regime",
        _format_pullback_group_rows(report.get("failure_by_regime"), "regime_state"),
        "Failure by lifecycle state",
        _format_pullback_group_rows(report.get("failure_by_lifecycle_state"), "lifecycle_current_state"),
        "Conversion rate by pullback grade",
        _format_pullback_grade_rows(report.get("conversion_rate_by_pullback_grade")),
        _warning_block(report),
    )


def _format_pullback_quality_distribution(report: Mapping[str, Any]) -> str:
    return _join_sections(
        "Pullback Quality Distribution",
        _metric_table((("Total pullback rows", report.get("total_pullback_rows")),)),
        _format_pullback_grade_rows(report.get("pullback_quality_grades")),
        _warning_block(report),
    )


def _format_pullback_depth_analysis(report: Mapping[str, Any]) -> str:
    depth_rows = [
        (
            row.get("pullback_failure_type"),
            row.get("samples"),
            row.get("average_depth"),
        )
        for row in _sequence(report.get("average_depth_by_failure_type"))
    ]
    band_rows = [
        (
            row.get("depth_band"),
            row.get("count"),
            _pct_value(row.get("percentage")),
            row.get("most_common_failure_type"),
        )
        for row in _sequence(report.get("depth_bands"))
    ]
    deepest_rows = [
        (
            row.get("symbol"),
            row.get("pullback_depth_ratio"),
            row.get("pullback_failure_type"),
            row.get("pullback_quality_grade"),
            row.get("regime_state"),
        )
        for row in _sequence(report.get("deepest_pullbacks"))
    ]
    return _join_sections(
        "Pullback Depth Analysis",
        _metric_table(
            (
                ("Depth samples", report.get("total_depth_samples")),
                ("Average depth", report.get("average_depth")),
            )
        ),
        "Average depth by failure type",
        _table(("failure_type", "samples", "avg_depth"), depth_rows),
        "Depth bands",
        _table(("band", "count", "pct", "top_failure"), band_rows),
        "Deepest pullbacks",
        _table(("symbol", "depth", "failure", "grade", "regime"), deepest_rows),
        _warning_block(report),
    )


def _format_pullback_lifecycle_dropoffs(report: Mapping[str, Any]) -> str:
    return _join_sections(
        "Pullback Lifecycle Dropoffs",
        _metric_table(
            (
                ("Total pullback failures", report.get("total_pullback_failures")),
                ("TOO_DEEP failures", report.get("too_deep_failures")),
                ("TOO_DEEP invalidated/cooldown", report.get("too_deep_invalidated_or_cooldown")),
            )
        ),
        "Failure by lifecycle state",
        _format_pullback_group_rows(report.get("failure_by_lifecycle_state"), "lifecycle_current_state"),
        "Failure by regime",
        _format_pullback_group_rows(report.get("failure_by_regime"), "regime_state"),
        _warning_block(report),
    )


def _format_pullback_grade_rows(rows: Any) -> str:
    grade_rows = [
        (
            row.get("pullback_quality_grade"),
            row.get("count"),
            row.get("valid_setup_count"),
            _pct_value(row.get("conversion_to_valid_pct")),
            row.get("average_depth"),
        )
        for row in _sequence(rows)
    ]
    return _table(("grade", "count", "valid", "valid_pct", "avg_depth"), grade_rows)


def _format_pullback_group_rows(rows: Any, key: str) -> str:
    table_rows = [
        (
            row.get(key),
            row.get("count"),
            row.get("most_common_failure_type"),
        )
        for row in _sequence(rows)
    ]
    return _table((key, "count", "top_failure"), table_rows)


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


def _top_group(value: Any, key: str) -> str:
    groups = _sequence(value)
    if not groups:
        return NA
    first = groups[0]
    if not isinstance(first, Mapping):
        return NA
    return f"{_display(first.get(key))} ({_display(first.get('count'))})"


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
