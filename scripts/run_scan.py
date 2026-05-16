from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.data.dtos import NA  # noqa: E402
from app.formatters.scanner_display import (  # noqa: E402
    DEFAULT_MAX_DISPLAY_RESULTS,
    DisplayBucket,
    display_fields,
    filter_ranked_results,
    format_scan_dashboard,
    format_symbol_card,
    format_symbol_compact_line,
    rank_scan_results,
    representative_strategy_diagnostics,
)
from app.formatters.telegram_formatter import format_telegram_strategy_output  # noqa: E402
from app.pipeline.scanner_runner import ScannerRunConfig, ScannerRunResult, ScannerRunner, ScannerSymbolResult  # noqa: E402
from app.watchlists.presets import (  # noqa: E402
    WatchlistPresetError,
    dedupe_symbols,
    load_custom_preset,
    preset_symbols,
    presets_with_counts,
    validate_symbols,
)


DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


@dataclass(frozen=True)
class WatchlistResolution:
    symbols: tuple[str, ...]
    source_label: str


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    tokens = list(sys.argv[1:] if argv is None else argv)
    symbols_explicit = any(token == "--symbols" or token.startswith("--symbols=") for token in tokens)
    diagnostics_level_explicit = any(
        token == "--diagnostics-level" or token.startswith("--diagnostics-level=") for token in tokens
    )
    display_explicit = any(token == "--display" or token.startswith("--display=") for token in tokens)
    parser = argparse.ArgumentParser(description="Run the Candle Craft dry-run scanner pipeline.")
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--preset")
    parser.add_argument("--list-presets", action="store_true")
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--include-symbols", nargs="*", default=[])
    parser.add_argument("--exclude-symbols", nargs="*", default=[])
    parser.add_argument("--preset-file", type=Path)
    parser.add_argument("--exchange", choices=["binance", "bybit"], default="binance")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--candle-limit", type=int, default=250)
    parser.add_argument("--account-equity", default="10000")
    parser.add_argument("--risk-per-trade-pct", default="1")
    parser.add_argument("--min-score-for-idea", default="80")
    parser.add_argument("--strategy", choices=["liquidity_grab_pullback"], default="liquidity_grab_pullback")
    parser.add_argument("--modes", nargs="+", choices=["challenge", "swing", "scalp"], default=["challenge", "swing", "scalp"])
    parser.add_argument("--htf-timeframe", default="2d")
    parser.add_argument("--bias-timeframe", default="12h")
    parser.add_argument("--execution-timeframe", default="15m")
    parser.add_argument("--confirmation-timeframe", default="5m")
    parser.add_argument("--aggressive-toggle", action="store_true")
    parser.add_argument("--show-strategy-output", action="store_true")
    parser.add_argument("--telegram-format", action="store_true")
    parser.add_argument("--diagnostics-level", choices=["summary", "normal", "full"], default="normal")
    parser.add_argument("--display", choices=["compact", "normal", "full"], default="normal")
    parser.add_argument("--rank-results", dest="rank_results", action="store_true", default=True)
    parser.add_argument("--no-rank-results", dest="rank_results", action="store_false")
    parser.add_argument("--show-no-setups", action="store_true")
    parser.add_argument("--max-display-results", type=int, default=DEFAULT_MAX_DISPLAY_RESULTS)
    parser.add_argument("--bucket-filter", nargs="+")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)
    args.symbols_explicit = symbols_explicit
    args.diagnostics_level_explicit = diagnostics_level_explicit
    args.display_explicit = display_explicit
    return args


async def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.list_presets:
        print(_format_available_presets())
        return

    watchlist = _resolve_watchlist(args)
    diagnostics_level = args.diagnostics_level
    if args.verbose and not args.diagnostics_level_explicit:
        diagnostics_level = "full"
    display_mode = args.display
    if (args.verbose or diagnostics_level == "full") and not args.display_explicit:
        display_mode = "full"

    config = ScannerRunConfig(
        symbols=watchlist.symbols,
        exchange=args.exchange,
        interval=args.interval,
        candle_limit=args.candle_limit,
        dry_run_alerts=True,
        account_equity=Decimal(args.account_equity),
        risk_per_trade_pct=Decimal(args.risk_per_trade_pct),
        min_score_for_idea=Decimal(args.min_score_for_idea),
        verbose=diagnostics_level == "full",
        strategy_name=args.strategy,
        strategy_modes=args.modes,
        enable_strategy_output=True,
        include_formatted_strategy_output=True,
        aggressive_toggle=args.aggressive_toggle,
        htf_timeframe=args.htf_timeframe,
        bias_timeframe=args.bias_timeframe,
        execution_timeframe=args.execution_timeframe,
        confirmation_timeframe=args.confirmation_timeframe,
    )

    print(f"Watchlist: {watchlist.source_label}")
    print(f"Symbols queued: {len(watchlist.symbols)}")
    print("")

    result = await ScannerRunner().run(config)
    bucket_filter = _parse_bucket_filter(args.bucket_filter)
    ranked_results = rank_scan_results(result.results, rank_results=args.rank_results)
    visible_results = filter_ranked_results(
        ranked_results,
        show_no_setups=args.show_no_setups,
        bucket_filter=bucket_filter,
        max_display_results=args.max_display_results,
    )

    if args.output_json is not None:
        args.output_json.write_text(
            json.dumps(_json_payload(result, ranked_results), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(format_scan_dashboard(result, ranked_results=ranked_results, visible_results=visible_results))
    print("")

    for ranked in visible_results:
        symbol_result = ranked.symbol_result
        if display_mode == "compact":
            print(format_symbol_compact_line(symbol_result, rank=ranked.display_rank))
        else:
            print(format_symbol_card(symbol_result, rank=ranked.display_rank))
            if display_mode == "full":
                print("")
                print(_format_symbol_diagnostics(symbol_result))
        if args.show_strategy_output:
            print("")
            print(f"{symbol_result.symbol} Candle Craft strategy output:")
            if args.telegram_format:
                print(format_telegram_strategy_output(symbol_result, diagnostics_level=diagnostics_level))
            else:
                print(_format_strategy_output_for_cli(symbol_result))


def _format_available_presets() -> str:
    lines = ["Available watchlist presets:"]
    for name, count in presets_with_counts():
        lines.append(f"- {name} ({count} symbols)")
    return "\n".join(lines)


def _resolve_watchlist(args: argparse.Namespace) -> WatchlistResolution:
    source_count = sum(
        (
            bool(args.symbols_explicit),
            bool(args.preset),
            args.preset_file is not None,
        )
    )
    if source_count > 1:
        raise SystemExit("Use only one watchlist source: --symbols, --preset, or --preset-file.")

    try:
        if args.symbols_explicit:
            symbols = validate_symbols(args.symbols, context="--symbols")
            source_label = "symbols"
        elif args.preset_file is not None:
            custom_preset = load_custom_preset(args.preset_file)
            symbols = custom_preset.symbols
            source_label = f"custom file {custom_preset.name}"
        elif args.preset:
            symbols = preset_symbols(args.preset)
            source_label = f"preset {args.preset.strip().lower()}"
        else:
            symbols = validate_symbols(args.symbols, context="default symbols")
            source_label = "symbols"

        include_symbols = validate_symbols(args.include_symbols, context="--include-symbols")
        exclude_symbols = set(validate_symbols(args.exclude_symbols, context="--exclude-symbols"))
    except WatchlistPresetError as exc:
        raise SystemExit(str(exc)) from exc

    resolved_symbols = dedupe_symbols((*symbols, *include_symbols))
    if exclude_symbols:
        resolved_symbols = tuple(symbol for symbol in resolved_symbols if symbol not in exclude_symbols)

    if args.max_symbols is not None:
        if args.max_symbols < 1:
            raise SystemExit("--max-symbols must be at least 1.")
        resolved_symbols = resolved_symbols[: args.max_symbols]

    if not resolved_symbols:
        raise SystemExit(
            "Resolved watchlist is empty after include/exclude/max-symbols processing. Provide at least one symbol."
        )

    return WatchlistResolution(symbols=resolved_symbols, source_label=source_label)


def _parse_bucket_filter(values: Sequence[str] | None) -> set[DisplayBucket] | None:
    if values is None:
        return None
    aliases: dict[str, DisplayBucket] = {
        "valid": "valid",
        "valid_setup": "valid",
        "near_miss": "near_miss",
        "near-miss": "near_miss",
        "no_setup": "no_setup",
        "no-setup": "no_setup",
        "data_issue": "data_issue",
        "data-issue": "data_issue",
        "data_incomplete": "data_issue",
    }
    buckets: set[DisplayBucket] = set()
    invalid: list[str] = []
    for raw_value in values:
        for item in raw_value.split(","):
            normalized = item.strip().lower()
            if not normalized:
                continue
            bucket = aliases.get(normalized)
            if bucket is None:
                invalid.append(item)
                continue
            buckets.add(bucket)
    if invalid:
        allowed = "valid,near_miss,no_setup,data_issue"
        raise SystemExit(f"--bucket-filter accepts {allowed}; invalid value(s): {', '.join(invalid)}")
    return buckets


def _json_payload(result: ScannerRunResult, ranked_results=None) -> dict[str, object]:
    payload = result.model_dump(mode="json")
    ranks_by_index = {}
    if ranked_results is None:
        ranked_results = rank_scan_results(result.results)
    for ranked in ranked_results:
        ranks_by_index[ranked.original_index] = ranked.display_rank
    for index, symbol_result in enumerate(result.results):
        payload["results"][index].update(display_fields(symbol_result, display_rank=ranks_by_index.get(index)))
    return payload


def _format_symbol_summary(symbol_result: ScannerSymbolResult) -> str:
    return format_symbol_compact_line(symbol_result)


def _format_symbol_normal_block(symbol_result: ScannerSymbolResult) -> str:
    return format_symbol_card(symbol_result)


def _format_symbol_diagnostics(symbol_result: ScannerSymbolResult) -> str:
    reason = _diagnostic_reason(symbol_result)
    return "\n".join(
        (
            symbol_result.symbol,
            f"Status: {symbol_result.status.value}",
            f"Latest close: {_display(symbol_result.latest_close)}",
            f"Trend: {_display(symbol_result.trend_context)}",
            f"Technical score: {_display(symbol_result.technical_score)}",
            f"Derivatives context score: {_display(symbol_result.derivatives_score)}",
            f"Range high: {_display(symbol_result.recent_range_high)}",
            f"Range low: {_display(symbol_result.recent_range_low)}",
            f"Latest swing high: {_display(symbol_result.latest_swing_high)}",
            f"Latest swing low: {_display(symbol_result.latest_swing_low)}",
            f"Sweep detected: {_bool_text(symbol_result.sweep_detected)}",
            f"BOS detected: {_bool_text(symbol_result.bos_detected)}",
            f"CHoCH detected: {_bool_text(symbol_result.choch_detected)}",
            f"Funding: {_display(symbol_result.funding_direction)} / {_display(symbol_result.funding_severity)}",
            f"Funding status: {_display(symbol_result.funding_status)}",
            f"Funding extreme: {_display(symbol_result.funding_extreme)}",
            f"Open interest: {_display(symbol_result.open_interest)}",
            f"Open interest change %: {_display(symbol_result.open_interest_change_pct)}",
            f"OI direction: {_display(symbol_result.oi_direction)}",
            f"Long/short ratio: {_display(symbol_result.long_short_ratio)}",
            f"Price/OI: {_display(symbol_result.price_oi_relationship)}",
            f"Crowding risk: {_display(symbol_result.crowding_risk)}",
            f"Squeeze risk: {_display(symbol_result.squeeze_risk)}",
            f"Derivatives missing data: {_sequence_text(symbol_result.derivatives_missing_data)}",
            f"Derivatives unverified data: {_sequence_text(symbol_result.derivatives_unverified_data)}",
            f"Derivatives warnings: {_sequence_text(symbol_result.derivatives_warnings)}",
            "Derivatives enrichment:",
            _format_derivatives_diagnostics(symbol_result),
            f"Volume profile source: {_display(symbol_result.volume_profile_source)}",
            f"POC: {_display(symbol_result.poc)}",
            f"Value area high: {_display(symbol_result.value_area_high)}",
            f"Value area low: {_display(symbol_result.value_area_low)}",
            f"Nearest high-volume node: {_display(symbol_result.nearest_high_volume_node)}",
            f"Nearest low-volume node: {_display(symbol_result.nearest_low_volume_node)}",
            f"Volume profile warnings: {_sequence_text(symbol_result.volume_profile_warnings)}",
            "Volume profile diagnostics:",
            _format_volume_profile_diagnostics(symbol_result),
            f"Rejection stage: {_display(symbol_result.rejection_stage)}",
            f"Reason: {reason}",
            f"Missing data: {_sequence_text(symbol_result.missing_data)}",
            f"Unverified data: {_sequence_text(symbol_result.unverified_data)}",
            f"Strategy: {_display(symbol_result.strategy_name)}",
            f"Valid strategy modes: {_sequence_text(symbol_result.valid_strategy_modes)}",
            f"Rejected strategy modes: {_sequence_text(symbol_result.rejected_strategy_modes)}",
            f"Strategy missing data: {_sequence_text(symbol_result.strategy_missing_data)}",
            f"Strategy unverified data: {_sequence_text(symbol_result.strategy_unverified_data)}",
            "Strategy diagnostics:",
            _format_strategy_diagnostics(symbol_result),
        )
    )


def _representative_strategy_diagnostics(symbol_result: ScannerSymbolResult) -> dict[str, object]:
    return dict(representative_strategy_diagnostics(symbol_result))


def _symbol_status_label(symbol_result: ScannerSymbolResult) -> str:
    if symbol_result.error_message:
        return "Failed"
    if symbol_result.trade_idea is not None or symbol_result.valid_strategy_modes:
        return "Setup"
    return "No Setup"


def _status_text(value: object) -> str:
    text = _display(value)
    if text == "not_evaluated":
        return "not evaluated"
    return text


def _execution_text(diagnostics: dict[str, object]) -> str:
    status = _status_text(diagnostics.get("execution_sweep_status"))
    sweep_text = _display(diagnostics.get("sweep_diagnostics")).lower()
    if status == "passed":
        if "bearish" in sweep_text:
            return "bearish sweep detected"
        if "bullish" in sweep_text:
            return "bullish sweep detected"
        return "sweep detected"
    if status == "failed":
        return "sweep failed"
    return status


def _confirmation_text(diagnostics: dict[str, object]) -> str:
    status = _status_text(diagnostics.get("confirmation_structure_shift_status"))
    if status == "passed":
        return "BOS/CHoCH passed"
    if status == "failed":
        return "BOS/CHoCH failed"
    return status


def _pullback_normal_text(diagnostics: dict[str, object]) -> str:
    status = _status_text(diagnostics.get("pullback_zone_status"))
    source = _display(diagnostics.get("pullback_calculation_timeframe"))
    selected = _display(diagnostics.get("selected_zone_type"))
    fib = _display(diagnostics.get("fib_alignment_status"))
    rr = _display(diagnostics.get("rr_to_tp2"))
    lines = [
        "Pullback:",
        f"Status: {status}",
    ]
    if source != NA:
        lines.append(f"Source: {source}")
    lines.extend(
        (
            f"OB/FVG: {selected}",
            f"Fib: {fib}",
            f"RR: {rr}",
        )
    )
    return "\n".join(lines)


def _normal_reason(symbol_result: ScannerSymbolResult, diagnostics: dict[str, object]) -> str:
    confirmation_reason = _display(diagnostics.get("confirmation_bos_choch_reason"))
    if confirmation_reason != NA and diagnostics.get("first_failed_gate") in (
        "missing_confirmation_structure_shift",
        "missing_confirmation_candles",
    ):
        return confirmation_reason
    pullback_reason = _display(diagnostics.get("pullback_failure_reason"))
    if pullback_reason != NA and _display(diagnostics.get("pullback_zone_status")) == "failed":
        return pullback_reason
    hard_rejections = diagnostics.get("hard_rejection_reasons")
    if isinstance(hard_rejections, Sequence) and not isinstance(hard_rejections, (str, bytes)) and hard_rejections:
        return str(hard_rejections[0])
    return _diagnostic_reason(symbol_result)


def _volume_profile_normal_text(symbol_result: ScannerSymbolResult) -> str:
    return (
        f"Volume Profile: POC [{_display(symbol_result.poc)}], "
        f"VAH [{_display(symbol_result.value_area_high)}], "
        f"VAL [{_display(symbol_result.value_area_low)}], "
        f"source {_display(symbol_result.volume_profile_source)}"
    )


def _compact_derivatives_context(symbol_result: ScannerSymbolResult) -> str:
    funding = _funding_summary(symbol_result)
    oi = _display(symbol_result.oi_direction)
    parts = []
    if funding != NA:
        parts.append(f"Funding: {funding}")
    if oi != NA:
        parts.append(f"OI: {oi}")
    return " | ".join(parts) if parts else NA


def _funding_summary(symbol_result: ScannerSymbolResult) -> str:
    status = _display(symbol_result.funding_status)
    rate = symbol_result.funding_rate
    if status == NA and rate == NA:
        return NA
    if isinstance(rate, Decimal):
        if rate > 0:
            direction = "positive"
        elif rate < 0:
            direction = "negative"
        else:
            direction = "neutral"
    else:
        direction = _display(symbol_result.funding_direction)
        if direction == NA:
            direction = NA

    severity = status
    if status in ("elevated_positive", "elevated_negative"):
        severity = "elevated"
    elif status in ("extreme_positive", "extreme_negative"):
        severity = "extreme"
    if direction == NA:
        return severity
    if severity == NA:
        return direction
    return f"{direction}/{severity}"


def _format_derivatives_normal_block(symbol_result: ScannerSymbolResult) -> str:
    return "\n".join(
        (
            "Derivatives:",
            f"Funding: [{_display(symbol_result.funding_rate)}] | status [{_funding_status_display(symbol_result.funding_status)}]",
            f"OI: [{_display(symbol_result.open_interest)}] | change [{_percentage_display(symbol_result.open_interest_change_pct)}] | direction [{_display(symbol_result.oi_direction)}]",
            f"Price/OI: [{_display(symbol_result.price_oi_relationship)}]",
            f"Crowding: [{_display(symbol_result.crowding_risk)}]",
            f"Squeeze: [{_display(symbol_result.squeeze_risk)}]",
            f"Context score: [{_display(symbol_result.derivatives_score)}]",
        )
    )


def _funding_status_display(value: object) -> str:
    status = _display(value)
    if status in ("elevated_positive", "elevated_negative"):
        return "elevated"
    if status in ("extreme_positive", "extreme_negative"):
        return "extreme"
    return status


def _percentage_display(value: object) -> str:
    text = _display(value)
    if text == NA:
        return NA
    return f"{text}%"


def _format_derivatives_diagnostics(symbol_result: ScannerSymbolResult) -> str:
    enrichment = symbol_result.derivatives_enrichment
    if enrichment is None:
        return NA
    return str(enrichment.model_dump())


def _format_volume_profile_diagnostics(symbol_result: ScannerSymbolResult) -> str:
    profile = symbol_result.volume_profile
    if profile is None:
        return NA
    lines = [
        f"{profile.timeframe}: source={profile.source}",
        f"{profile.timeframe}: POC={_display(profile.poc)}, VAH={_display(profile.value_area_high)}, VAL={_display(profile.value_area_low)}",
        f"{profile.timeframe}: range={_display(profile.price_range_low)} - {_display(profile.price_range_high)}, total_volume={_display(profile.total_volume)}, candles_used={profile.candles_used}",
        f"{profile.timeframe}: nearest HVN={_display(profile.nearest_high_volume_node)}, nearest LVN={_display(profile.nearest_low_volume_node)}",
        f"{profile.timeframe}: HVNs={_volume_profile_nodes_text(profile.high_volume_nodes)}",
        f"{profile.timeframe}: LVNs={_volume_profile_nodes_text(profile.low_volume_nodes)}",
        f"{profile.timeframe}: warnings={_sequence_text(profile.warnings)}",
    ]
    if symbol_result.volume_profile_12h is not None:
        profile_12h = symbol_result.volume_profile_12h
        lines.append(
            f"12h: POC={_display(profile_12h.poc)}, VAH={_display(profile_12h.value_area_high)}, "
            f"VAL={_display(profile_12h.value_area_low)}, source={profile_12h.source}"
        )
    return "\n".join(lines)


def _volume_profile_nodes_text(nodes: Sequence[object]) -> str:
    values: list[str] = []
    for node in nodes:
        price = getattr(node, "price", NA)
        volume = getattr(node, "volume", NA)
        values.append(f"{_display(price)} vol {_display(volume)}")
    return ", ".join(values) if values else NA


def _format_strategy_output_for_cli(symbol_result: ScannerSymbolResult) -> str:
    if not symbol_result.valid_strategy_modes:
        return "\n".join(
            (
                "Challenge: No valid challenge setup.",
                "Swing: No valid swing setup.",
                "Scalp: No valid scalp setup.",
            )
        )
    return _display(symbol_result.formatted_strategy_output)


def _diagnostic_reason(symbol_result: ScannerSymbolResult) -> str:
    if symbol_result.rejection_reasons:
        return "; ".join(symbol_result.rejection_reasons)
    if symbol_result.error_message:
        return symbol_result.error_message
    if symbol_result.journal_entry is not None:
        return "Journal entry created after scanner gates passed."
    if symbol_result.alert_result is not None:
        return "Dry-run alert created after scanner gates passed."
    if symbol_result.trade_idea is not None:
        return "Valid setup created after scanner gates passed."
    return "N/A"


def _sequence_text(values: Sequence[str]) -> str:
    return ", ".join(values) if values else NA


def _format_strategy_diagnostics(symbol_result: ScannerSymbolResult) -> str:
    if not symbol_result.strategy_diagnostics:
        return NA

    lines: list[str] = []
    for mode, diagnostics in symbol_result.strategy_diagnostics.items():
        if not isinstance(diagnostics, dict):
            lines.append(f"{mode}: {_display(diagnostics)}")
            continue

        failed_gates = _sequence_text(tuple(str(value) for value in diagnostics.get("gates_failed", ())))
        hard_rejections = _sequence_text(tuple(str(value) for value in diagnostics.get("hard_rejection_reasons", ())))
        candles_12h_count = int(diagnostics.get("candles_12h_count") or 0)
        htf_timeframe = _display(diagnostics.get("htf_timeframe"))
        bias_timeframe = _display(diagnostics.get("bias_timeframe"))
        execution_timeframe = _display(diagnostics.get("execution_timeframe"))
        confirmation_timeframe = _display(diagnostics.get("confirmation_timeframe"))
        lines.extend(
            (
                f"{mode}: valid={_bool_text(bool(diagnostics.get('is_valid')))} "
                f"trust={_display(diagnostics.get('trust_grade'))} "
                f"{_display(diagnostics.get('trust_percentage'))}%",
                f"{mode} {htf_timeframe.upper()} context: {_context_source_text(_display(diagnostics.get('htf_2d_context_source')))}",
                f"{mode} {bias_timeframe.upper()} bias: {'direct' if candles_12h_count > 0 else NA}",
                f"{mode} candles: 2D={_display(diagnostics.get('candles_2d_count'))}, "
                f"12H={_display(diagnostics.get('candles_12h_count'))}, "
                f"15m={_display(diagnostics.get('candles_15m_count'))}, "
                f"5m={_display(diagnostics.get('candles_5m_count'))}",
                f"{mode} HTF/MTF trend: 2D={_display(diagnostics.get('htf_2d_trend'))}, "
                f"12H={_display(diagnostics.get('mtf_12h_trend'))}",
                f"{mode} {execution_timeframe} execution sweep: {_status_text(diagnostics.get('execution_sweep_status'))}",
                f"{mode} {confirmation_timeframe} confirmation BOS/CHoCH: "
                f"{_status_text(diagnostics.get('confirmation_structure_shift_status'))}",
                f"{mode} volume profile source: {_display(diagnostics.get('volume_profile_source'))}",
                f"{mode} POC: {_display(diagnostics.get('poc'))}",
                f"{mode} POC diagnostics: {_display(diagnostics.get('poc_diagnostics'))}",
                f"{mode} confirmation reason: {_display(diagnostics.get('confirmation_bos_choch_reason'))}",
                f"{mode} Pullback Zone: {_display(diagnostics.get('pullback_zone_status'))} | "
                f"OB/FVG: {_display(diagnostics.get('selected_zone_type'))} | "
                f"Fib: {_display(diagnostics.get('fib_alignment_status'))} | "
                f"RR: {_display(diagnostics.get('rr_to_tp2'))}",
                f"{mode} pullback source: {_display(diagnostics.get('pullback_calculation_timeframe'))} | "
                f"sweep index {_display(diagnostics.get('pullback_sweep_candle_index'))} | "
                f"BOS/CHoCH index {_display(diagnostics.get('pullback_bos_choch_candle_index'))}",
                f"{mode} pullback failure reason: {_display(diagnostics.get('pullback_failure_reason'))}",
                f"{mode} OB zone: {_display(diagnostics.get('ob_zone'))}",
                f"{mode} FVG zone: {_display(diagnostics.get('fvg_zone'))}",
                f"{mode} first failed gate: {_display(diagnostics.get('first_failed_gate'))}",
                f"{mode} final decision: {'valid setup' if diagnostics.get('is_valid') else 'no setup'}",
                f"{mode} failed gates: {failed_gates}",
                f"{mode} hard rejections: {hard_rejections}",
                f"{mode} sweep: {_display(diagnostics.get('sweep_diagnostics'))}",
                f"{mode} BOS/CHoCH: {_display(diagnostics.get('bos_choch_diagnostics'))}",
                f"{mode} OB/FVG: {_display(diagnostics.get('ob_fvg_diagnostics'))}",
                f"{mode} fib: {_display(diagnostics.get('fib_diagnostics'))}",
                f"{mode} RR: {_display(diagnostics.get('rr_diagnostics'))}",
                f"{mode} Trust Meter: {_display(diagnostics.get('trust_meter_diagnostics'))}",
                f"{mode} derivatives support: {_display(diagnostics.get('derivatives_supports_trade'))}",
                f"{mode} derivatives conflict: {_display(diagnostics.get('derivatives_conflict_reason'))}",
                f"{mode} funding context: {_display(diagnostics.get('funding_context'))}",
                f"{mode} OI context: {_display(diagnostics.get('oi_context'))}",
                f"{mode} crowding risk: {_display(diagnostics.get('crowding_risk'))}",
                f"{mode} squeeze risk: {_display(diagnostics.get('squeeze_risk'))}",
            )
        )
    return "\n".join(lines)


def _bool_text(value: bool) -> str:
    return str(value).lower()


def _display(value: object) -> str:
    if value is None or value == "":
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _context_source_text(value: str) -> str:
    if value == "synthetic_from_1d":
        return "synthetic from 1D"
    return value if value else NA


def _configure_cli_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


if __name__ == "__main__":
    _configure_cli_encoding()
    asyncio.run(main())
