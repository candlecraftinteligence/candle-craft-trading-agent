from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backtesting import (  # noqa: E402
    ReplayConfig,
    ReplaySummary,
    StrategyReplayEngine,
    backtest_json_payload,
    format_replay_summary,
)
from app.data.dtos import NA  # noqa: E402
from app.cache.market_data_cache import MarketDataCache  # noqa: E402
from app.formatters.scanner_display import (  # noqa: E402
    DEFAULT_MAX_DISPLAY_RESULTS,
    DisplayBucket,
    build_symbol_display,
    display_fields,
    filter_ranked_results,
    format_scan_dashboard,
    format_symbol_card,
    format_symbol_compact_line,
    rank_scan_results,
    representative_strategy_diagnostics,
)
from app.formatters.telegram_formatter import format_telegram_strategy_output  # noqa: E402
from app.pipeline.scanner_runner import (  # noqa: E402
    BINANCE_KLINE_LIMIT_MAX,
    BINANCE_KLINE_LIMIT_MIN,
    DEFAULT_REPLAY_CANDLES,
    DEFAULT_REQUEST_TIMEOUT_SEC,
    DEFAULT_SYMBOL_TIMEOUT_SEC,
    FAST_CANDLE_LIMIT,
    FAST_REPLAY_CANDLES,
    SAFE_REPLAY_CANDLE_LIMIT_MAX,
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerRuntimeStats,
    ScannerRunner,
    ScannerSymbolResult,
)
from app.universe.symbol_universe import (  # noqa: E402
    MANUAL_UNIVERSE_MODE,
    UNIVERSE_MODES,
    SymbolUniverse,
    UniverseResolutionError,
    manual_symbol_universe,
    resolve_symbol_universe,
)
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
    universe: SymbolUniverse


@dataclass(frozen=True)
class ResumeState:
    results_by_symbol: dict[str, ScannerSymbolResult]
    skipped_symbols: tuple[str, ...]
    loaded_symbols: tuple[str, ...]


def _non_negative_decimal_arg(value: str) -> Decimal:
    try:
        decimal = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a decimal number") from exc
    if not decimal.is_finite() or decimal < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return decimal


def _positive_float_arg(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    tokens = list(sys.argv[1:] if argv is None else argv)
    symbols_explicit = any(token == "--symbols" or token.startswith("--symbols=") for token in tokens)
    diagnostics_level_explicit = any(
        token == "--diagnostics-level" or token.startswith("--diagnostics-level=") for token in tokens
    )
    display_explicit = any(token == "--display" or token.startswith("--display=") for token in tokens)
    parser = argparse.ArgumentParser(description="Run the Candle Craft dry-run scanner pipeline.")
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--universe", choices=UNIVERSE_MODES, default=MANUAL_UNIVERSE_MODE)
    parser.add_argument("--universe-size", type=int, default=50)
    parser.add_argument("--min-quote-volume", type=_non_negative_decimal_arg, default=Decimal("0"))
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
    parser.add_argument("--replay", "--backtest", dest="replay", action="store_true")
    parser.add_argument("--replay-candles", "--backtest-candles", dest="replay_candles", type=int, default=DEFAULT_REPLAY_CANDLES)
    parser.add_argument("--same-candle-policy", choices=["conservative", "optimistic"], default="conservative")
    parser.add_argument("--replay-max-hold-candles", type=int)
    parser.add_argument("--replay-max-fill-candles", type=int)
    parser.add_argument("--backtest-max-setups", type=int)
    parser.add_argument("--backtest-output-json", type=Path)
    parser.add_argument("--backtest-summary-only", action="store_true")
    parser.add_argument("--request-timeout-sec", type=_positive_float_arg, default=DEFAULT_REQUEST_TIMEOUT_SEC)
    parser.add_argument("--symbol-timeout-sec", type=_positive_float_arg, default=DEFAULT_SYMBOL_TIMEOUT_SEC)
    parser.add_argument("--scan-timeout-sec", "--max-scan-seconds", dest="scan_timeout_sec", type=_positive_float_arg)
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--show-strategy-output", action="store_true")
    parser.add_argument("--telegram-format", action="store_true")
    parser.add_argument("--diagnostics-level", choices=["summary", "normal", "full"], default="normal")
    parser.add_argument("--display", choices=["compact", "normal", "full"], default="normal")
    parser.add_argument("--rank-results", dest="rank_results", action="store_true", default=True)
    parser.add_argument("--no-rank-results", dest="rank_results", action="store_false")
    parser.add_argument("--show-no-setups", action="store_true")
    parser.add_argument("--max-display-results", type=int, default=DEFAULT_MAX_DISPLAY_RESULTS)
    parser.add_argument("--bucket-filter", nargs="+")
    parser.add_argument(
        "--show-near-miss-plan",
        action="store_true",
        help="Print the near-miss plan block even when compact display is selected.",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--cache", dest="cache_enabled", action="store_true", default=True)
    parser.add_argument("--no-cache", dest="cache_enabled", action="store_false")
    parser.add_argument("--cache-ttl-seconds", type=int)
    parser.add_argument("--cache-file", type=Path)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--save-run", nargs="?", const=Path("scan_runs/latest_scan.json"), type=Path)
    parser.add_argument("--no-resume-skip", action="store_true")
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args(argv)
    if args.universe_size < 1:
        parser.error("--universe-size must be at least 1.")
    if args.cache_ttl_seconds is not None and args.cache_ttl_seconds < 0:
        parser.error("--cache-ttl-seconds must be zero or greater.")
    if args.replay_candles < 1:
        parser.error("--replay-candles must be at least 1.")
    if args.replay_max_hold_candles is not None and args.replay_max_hold_candles < 1:
        parser.error("--replay-max-hold-candles must be at least 1.")
    if args.replay_max_fill_candles is not None and args.replay_max_fill_candles < 1:
        parser.error("--replay-max-fill-candles must be at least 1.")
    if args.backtest_max_setups is not None and args.backtest_max_setups < 1:
        parser.error("--backtest-max-setups must be at least 1.")
    if args.backtest_output_json is not None:
        args.replay = True
    args.symbols_explicit = symbols_explicit
    args.diagnostics_level_explicit = diagnostics_level_explicit
    args.display_explicit = display_explicit
    return args


async def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.list_presets:
        print(_format_available_presets())
        return

    watchlist = await _resolve_watchlist_for_scan(args)
    diagnostics_level = args.diagnostics_level
    if args.verbose and not args.diagnostics_level_explicit:
        diagnostics_level = "full"
    display_mode = args.display
    if (args.verbose or diagnostics_level == "full") and not args.display_explicit:
        display_mode = "full"
    effective_candle_limit = _effective_candle_limit(args)

    config = ScannerRunConfig(
        symbols=watchlist.symbols,
        exchange=args.exchange,
        interval=args.interval,
        candle_limit=effective_candle_limit,
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
        cache_enabled=args.cache_enabled,
        cache_ttl_seconds=args.cache_ttl_seconds,
        cache_file=args.cache_file,
        request_timeout_sec=args.request_timeout_sec,
        symbol_timeout_sec=args.symbol_timeout_sec,
        scan_timeout_sec=args.scan_timeout_sec,
        fast_mode=args.fast,
    )

    print(_format_universe_header(watchlist.universe))
    print(f"Watchlist: {watchlist.source_label}")
    print(f"Symbols queued: {len(watchlist.symbols)}")
    for warning in _startup_warnings(args, effective_candle_limit):
        print(f"Warning: {warning}")
    print("")

    resume_state = _load_resume_state(args.resume_from, watchlist.symbols, skip_completed=not args.no_resume_skip)
    if args.progress and resume_state.skipped_symbols:
        print(f"Resume: skipped {len(resume_state.skipped_symbols)} completed symbol(s).")

    cache = (
        MarketDataCache(enabled=True, ttl_seconds=args.cache_ttl_seconds, file_path=args.cache_file)
        if args.cache_enabled
        else None
    )
    latest_results_by_symbol = dict(resume_state.results_by_symbol)
    symbols_to_scan = tuple(symbol for symbol in watchlist.symbols if symbol not in resume_state.skipped_symbols)
    scan_config = (
        ScannerRunConfig.model_validate({**config.model_dump(), "symbols": list(symbols_to_scan)})
        if symbols_to_scan
        else config
    )
    resume_metadata = _resume_metadata(args, watchlist.symbols, resume_state, symbols_to_scan, watchlist.universe)

    async def after_symbol(symbol_result: ScannerSymbolResult, completed: int, total: int) -> None:
        latest_results_by_symbol[symbol_result.symbol] = symbol_result
        if args.progress:
            print(_progress_line(symbol_result, completed=completed, total=total))
        if args.save_run is not None:
            partial_result = _combined_run_result(
                config=config,
                watchlist_symbols=watchlist.symbols,
                results_by_symbol=latest_results_by_symbol,
                cache=cache,
                retry_diagnostics=(),
                resume_metadata={
                    **resume_metadata,
                    "pending_symbols": [
                        symbol for symbol in watchlist.symbols if symbol not in latest_results_by_symbol
                    ],
                },
                runtime_stats=None,
            )
            _write_run_json(args.save_run, partial_result)

    async def progress(message: str) -> None:
        print(message, flush=True)

    if symbols_to_scan:
        runner = _scanner_runner(cache)
        scan_result = await _run_scanner(
            runner,
            scan_config,
            after_symbol=after_symbol if (args.progress or args.save_run is not None) else None,
            progress=progress,
            resume_metadata=resume_metadata,
        )
        for symbol_result in scan_result.results:
            latest_results_by_symbol[symbol_result.symbol] = symbol_result
        result = _combined_run_result(
            config=config,
            watchlist_symbols=watchlist.symbols,
            results_by_symbol=latest_results_by_symbol,
            cache=cache,
            retry_diagnostics=scan_result.retry_diagnostics,
            resume_metadata={
                **resume_metadata,
                "pending_symbols": [
                    symbol for symbol in watchlist.symbols if symbol not in latest_results_by_symbol
                ],
            },
            runtime_stats=scan_result.runtime_stats,
        )
    else:
        result = _combined_run_result(
            config=config,
            watchlist_symbols=watchlist.symbols,
            results_by_symbol=latest_results_by_symbol,
            cache=cache,
            retry_diagnostics=(),
            resume_metadata={**resume_metadata, "pending_symbols": []},
            runtime_stats=None,
        )

    if args.save_run is not None:
        _write_run_json(args.save_run, result)

    replay_summary: ReplaySummary | None = None
    if args.replay:
        replay_summary = await _run_replay(args, watchlist, config, cache)

    bucket_filter = _parse_bucket_filter(args.bucket_filter)
    ranked_results = rank_scan_results(result.results, rank_results=args.rank_results)
    visible_results = filter_ranked_results(
        ranked_results,
        show_no_setups=args.show_no_setups,
        bucket_filter=bucket_filter,
        max_display_results=args.max_display_results,
    )

    if args.output_json is not None:
        _write_run_json(args.output_json, result, ranked_results=ranked_results, replay_summary=replay_summary)
    if args.backtest_output_json is not None and replay_summary is not None:
        _write_backtest_json(args.backtest_output_json, replay_summary)

    print(format_scan_dashboard(result, ranked_results=ranked_results, visible_results=visible_results))
    if replay_summary is not None:
        print("")
        print(
            format_replay_summary(
                replay_summary,
                summary_only=args.backtest_summary_only,
                include_setup_diagnostics=diagnostics_level == "full" or display_mode == "full",
            )
        )
    if diagnostics_level == "full" or display_mode == "full":
        print("")
        print(_format_run_diagnostics(result))
    print("")

    for ranked in visible_results:
        symbol_result = ranked.symbol_result
        if display_mode == "compact":
            print(format_symbol_compact_line(symbol_result, rank=ranked.display_rank))
            if args.show_near_miss_plan and ranked.display.display_bucket == "near_miss":
                print("")
                print(format_symbol_card(symbol_result, rank=ranked.display_rank))
        else:
            print(
                format_symbol_card(
                    symbol_result,
                    include_diagnostics=display_mode == "full" and ranked.display.display_bucket == "near_miss",
                    rank=ranked.display_rank,
                )
            )
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


def _effective_candle_limit(args: argparse.Namespace) -> int:
    if args.fast and args.candle_limit > FAST_CANDLE_LIMIT:
        return FAST_CANDLE_LIMIT
    return args.candle_limit


def _effective_replay_candles(args: argparse.Namespace) -> int:
    replay_candles = min(args.replay_candles, SAFE_REPLAY_CANDLE_LIMIT_MAX)
    if args.fast:
        replay_candles = min(replay_candles, FAST_REPLAY_CANDLES)
    return replay_candles


def _startup_warnings(args: argparse.Namespace, effective_candle_limit: int) -> tuple[str, ...]:
    warnings: list[str] = []
    if args.fast and effective_candle_limit != args.candle_limit:
        warnings.append(f"Fast mode clamped candle limit from {args.candle_limit} to {effective_candle_limit}.")
    if args.replay and args.replay_candles > SAFE_REPLAY_CANDLE_LIMIT_MAX:
        warnings.append(
            f"Replay candles {args.replay_candles} is high; clamped to {SAFE_REPLAY_CANDLE_LIMIT_MAX} per timeframe."
        )
    if args.replay and args.fast and min(args.replay_candles, SAFE_REPLAY_CANDLE_LIMIT_MAX) > FAST_REPLAY_CANDLES:
        warnings.append(f"Fast mode clamped replay candles to {FAST_REPLAY_CANDLES}.")
    return tuple(warnings)


def _format_universe_header(universe: SymbolUniverse) -> str:
    top_market_cap_symbols = universe.top_by_market_cap_rank(limit=5)
    top_quote_volume_symbols = universe.top_by_quote_volume(limit=5)
    if top_market_cap_symbols:
        top_text = ", ".join(
            f"{item.symbol} (rank {item.rank}, market cap {_display(item.market_cap)})"
            for item in top_market_cap_symbols
        )
        top_label = "Top 5 by public market-cap rank"
    elif top_quote_volume_symbols:
        top_text = ", ".join(f"{item.symbol} ({_display(item.quote_volume)})" for item in top_quote_volume_symbols)
        top_label = "Top 5 by quote volume"
    else:
        top_text = "N/A"
        top_label = "Top 5"
    return "\n".join(
        (
            f"Universe mode: {universe.mode}",
            f"Universe label: {universe.label}",
            f"Universe source: {universe.source}",
            f"Universe size requested: {universe.requested_size}",
            f"Symbols resolved: {len(universe.resolved_symbols)}",
            f"Excluded count: {len(universe.excluded_symbols)}",
            f"{top_label}: {top_text}",
        )
    )


async def _resolve_watchlist_for_scan(args: argparse.Namespace) -> WatchlistResolution:
    if args.universe == MANUAL_UNIVERSE_MODE:
        return _resolve_watchlist(args)
    return await _resolve_universe_watchlist(args)


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

    pre_exclude_symbols = dedupe_symbols((*symbols, *include_symbols))
    excluded_cli_symbols = tuple(symbol for symbol in pre_exclude_symbols if symbol in exclude_symbols)
    resolved_symbols = pre_exclude_symbols
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

    universe = manual_symbol_universe(
        resolved_symbols,
        requested_size=len(resolved_symbols),
        excluded_symbols=excluded_cli_symbols,
        min_quote_volume=args.min_quote_volume,
    )
    return WatchlistResolution(symbols=resolved_symbols, source_label=source_label, universe=universe)


async def _resolve_universe_watchlist(args: argparse.Namespace) -> WatchlistResolution:
    if args.exchange != "binance":
        raise SystemExit("Binance symbol universes require --exchange binance.")
    if args.preset or args.preset_file is not None:
        raise SystemExit("Use either --universe or manual watchlist sources such as --preset/--preset-file, not both.")

    try:
        universe = await resolve_symbol_universe(
            args.universe,
            universe_size=args.universe_size,
            min_quote_volume=args.min_quote_volume,
        )
        include_symbols = validate_symbols(args.include_symbols, context="--include-symbols")
        exclude_symbols = set(validate_symbols(args.exclude_symbols, context="--exclude-symbols"))
    except WatchlistPresetError as exc:
        raise SystemExit(str(exc)) from exc
    except UniverseResolutionError as exc:
        raise SystemExit(str(exc)) from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    pre_exclude_symbols = dedupe_symbols((*universe.resolved_symbols, *include_symbols))
    excluded_cli_symbols = tuple(symbol for symbol in pre_exclude_symbols if symbol in exclude_symbols)
    resolved_symbols = pre_exclude_symbols
    if exclude_symbols:
        resolved_symbols = tuple(symbol for symbol in resolved_symbols if symbol not in exclude_symbols)

    if args.max_symbols is not None:
        if args.max_symbols < 1:
            raise SystemExit("--max-symbols must be at least 1.")
        resolved_symbols = resolved_symbols[: args.max_symbols]

    if not resolved_symbols:
        raise SystemExit(
            "Resolved watchlist is empty after universe/include/exclude/max-symbols processing. Provide at least one symbol."
        )

    universe = universe.with_resolved_symbols(resolved_symbols, extra_excluded_symbols=excluded_cli_symbols)
    return WatchlistResolution(
        symbols=resolved_symbols,
        source_label=f"universe {args.universe}",
        universe=universe,
    )


def _load_resume_state(
    path: Path | None,
    watchlist_symbols: Sequence[str],
    *,
    skip_completed: bool,
) -> ResumeState:
    if path is None:
        return ResumeState(results_by_symbol={}, skipped_symbols=(), loaded_symbols=())
    if not path.exists():
        raise SystemExit(f"--resume-from file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--resume-from must be valid JSON: {path}") from exc

    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list):
        raise SystemExit("--resume-from JSON must contain a results list.")

    watchlist_set = set(watchlist_symbols)
    results_by_symbol: dict[str, ScannerSymbolResult] = {}
    for raw_result in raw_results:
        try:
            symbol_result = ScannerSymbolResult.model_validate(raw_result)
        except Exception:
            continue
        if symbol_result.symbol in watchlist_set:
            results_by_symbol[symbol_result.symbol] = symbol_result

    skipped_symbols = tuple(
        symbol
        for symbol in watchlist_symbols
        if skip_completed
        and symbol in results_by_symbol
        and _resume_result_is_completed(results_by_symbol[symbol])
    )
    return ResumeState(
        results_by_symbol=results_by_symbol,
        skipped_symbols=skipped_symbols,
        loaded_symbols=tuple(results_by_symbol.keys()),
    )


def _resume_result_is_completed(symbol_result: ScannerSymbolResult) -> bool:
    if symbol_result.error_message:
        return False
    return symbol_result.status not in (ScannerPipelineStatus.SCAN_ERROR, ScannerPipelineStatus.FAILED)


def _resume_metadata(
    args: argparse.Namespace,
    watchlist_symbols: Sequence[str],
    resume_state: ResumeState,
    symbols_to_scan: Sequence[str],
    universe: SymbolUniverse,
) -> dict[str, Any]:
    return {
        "resume_from": str(args.resume_from) if args.resume_from is not None else None,
        "save_run": str(args.save_run) if args.save_run is not None else None,
        "resume_skip_enabled": not args.no_resume_skip,
        "loaded_symbols": list(resume_state.loaded_symbols),
        "skipped_symbols": list(resume_state.skipped_symbols),
        "symbols_to_scan": list(symbols_to_scan),
        "watchlist_symbols": list(watchlist_symbols),
        "universe": universe.to_json(),
    }


def _combined_run_result(
    *,
    config: ScannerRunConfig,
    watchlist_symbols: Sequence[str],
    results_by_symbol: dict[str, ScannerSymbolResult],
    cache: MarketDataCache | None,
    retry_diagnostics: Sequence[dict[str, Any]],
    resume_metadata: Mapping[str, Any],
    runtime_stats: ScannerRuntimeStats | None,
) -> ScannerRunResult:
    ordered_results = tuple(
        results_by_symbol[symbol]
        for symbol in watchlist_symbols
        if symbol in results_by_symbol
    )
    cache_stats = cache.stats() if cache is not None else _empty_cache_stats(config)
    return ScannerRunResult(
        config=config,
        results=ordered_results,
        scanned_symbols=len(ordered_results),
        failed_symbols=sum(1 for result in ordered_results if _result_is_scan_error(result)),
        trade_ideas_created=sum(1 for result in ordered_results if result.trade_idea is not None),
        dry_run_alerts_created=sum(
            1 for result in ordered_results if ScannerPipelineStatus.ALERT_DRY_RUN_CREATED in result.status_history
        ),
        journal_entries_created=sum(1 for result in ordered_results if result.journal_entry is not None),
        cache_stats=cache_stats,
        retry_diagnostics=tuple(dict(event) for event in retry_diagnostics),
        resume_metadata=dict(resume_metadata),
        runtime_stats=_combined_runtime_stats(
            ordered_results,
            total_symbols=len(watchlist_symbols),
            runtime_stats=runtime_stats,
        ),
    )


def _empty_cache_stats(config: ScannerRunConfig) -> dict[str, Any]:
    return {
        "enabled": config.cache_enabled,
        "file_cache_enabled": config.cache_enabled and config.cache_file is not None,
        "file_path": str(config.cache_file) if config.cache_file is not None else None,
        "hits": 0,
        "misses": 0,
        "expired": 0,
        "writes": 0,
        "errors": 0,
        "entries": 0,
    }


def _combined_runtime_stats(
    results: Sequence[ScannerSymbolResult],
    *,
    total_symbols: int,
    runtime_stats: ScannerRuntimeStats | None,
) -> ScannerRuntimeStats:
    runtimes = tuple(
        (result.symbol, result.runtime_seconds)
        for result in results
        if result.runtime_seconds is not None
    )
    slowest_symbol = NA
    slowest_seconds = 0.0
    if runtimes:
        slowest_symbol, slowest_seconds = max(runtimes, key=lambda item: item[1])

    errored_symbols = sum(1 for result in results if _result_is_scan_error(result))
    skipped_symbols = max(0, total_symbols - len(results))
    timeout_count = sum(1 for result in results if result.timed_out)
    total_runtime = (
        runtime_stats.total_runtime_seconds
        if runtime_stats is not None
        else sum(seconds for _symbol, seconds in runtimes)
    )
    global_timeout_hit = runtime_stats.global_timeout_hit if runtime_stats is not None else False
    return ScannerRuntimeStats(
        total_runtime_seconds=_round_seconds(total_runtime),
        average_seconds_per_symbol=_round_seconds(sum(seconds for _symbol, seconds in runtimes) / len(runtimes))
        if runtimes
        else 0.0,
        slowest_symbol=slowest_symbol,
        slowest_symbol_seconds=_round_seconds(slowest_seconds),
        timeout_count=timeout_count,
        completed_symbols=max(0, len(results) - errored_symbols),
        skipped_symbols=skipped_symbols,
        errored_symbols=errored_symbols,
        skipped_errored_symbols=skipped_symbols + errored_symbols,
        global_timeout_hit=global_timeout_hit,
    )


def _round_seconds(value: float) -> float:
    return round(max(float(value), 0.0), 3)


def _result_is_scan_error(symbol_result: ScannerSymbolResult) -> bool:
    return symbol_result.status in (ScannerPipelineStatus.SCAN_ERROR, ScannerPipelineStatus.FAILED)


def _write_run_json(
    path: Path,
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[Any] | None = None,
    replay_summary: ReplaySummary | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_payload(result, ranked_results, replay_summary=replay_summary), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_backtest_json(path: Path, replay_summary: ReplaySummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(backtest_json_payload(replay_summary), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _scanner_runner(cache: MarketDataCache | None) -> Any:
    try:
        return ScannerRunner(market_data_cache=cache)
    except TypeError:
        return ScannerRunner()


async def _run_scanner(
    runner: Any,
    config: ScannerRunConfig,
    *,
    after_symbol: Any | None,
    progress: Any | None,
    resume_metadata: Mapping[str, Any],
) -> ScannerRunResult:
    try:
        return await runner.run(config, after_symbol=after_symbol, progress=progress, resume_metadata=resume_metadata)
    except TypeError:
        if after_symbol is not None:
            try:
                return await runner.run(config, after_symbol=after_symbol, resume_metadata=resume_metadata)
            except TypeError:
                pass
        if after_symbol is None:
            return await runner.run(config)
        result = await runner.run(config)
        total = len(result.results)
        for index, symbol_result in enumerate(result.results, start=1):
            await after_symbol(symbol_result, index, total)
        return result


async def _run_replay(
    args: argparse.Namespace,
    watchlist: WatchlistResolution,
    scanner_config: ScannerRunConfig,
    cache: MarketDataCache | None,
) -> ReplaySummary:
    replay_candles = _effective_replay_candles(args)
    replay_scan_config = ScannerRunConfig.model_validate(
        {
            **scanner_config.model_dump(),
            "symbols": list(watchlist.symbols),
            "interval": args.execution_timeframe,
            "replay_candles": replay_candles,
        }
    )
    runner = _scanner_runner(cache)
    client, owns_client = runner._exchange_client_for(replay_scan_config)
    candles_by_symbol: dict[str, Mapping[str, Sequence[Any]]] = {}
    timeframe_context_by_symbol: dict[str, Mapping[str, Any]] = {}
    data_notes_by_symbol: dict[str, Sequence[str]] = {}
    replay_deadline = time.monotonic() + args.scan_timeout_sec if args.scan_timeout_sec is not None else None

    try:
        for symbol in watchlist.symbols:
            if replay_deadline is not None and time.monotonic() >= replay_deadline:
                break
            remaining = (
                max(replay_deadline - time.monotonic(), 0.001)
                if replay_deadline is not None
                else args.symbol_timeout_sec
            )
            symbol_timeout = min(args.symbol_timeout_sec, remaining)
            try:
                (
                    candles_by_timeframe,
                    missing_data,
                    timeframe_context,
                    limit_warnings,
                ) = await asyncio.wait_for(
                    _fetch_replay_symbol_timeframes(
                        runner=runner,
                        client=client,
                        symbol=symbol,
                        args=args,
                        config=replay_scan_config,
                    ),
                    timeout=symbol_timeout,
                )
            except Exception as exc:
                message = _clean_error_message(exc)
                candles_by_timeframe = {}
                missing_data = (f"replay_{symbol}: N/A",)
                timeframe_context = {}
                limit_warnings = (f"replay unavailable for {symbol}: {message}",)

            candles_by_symbol[symbol] = candles_by_timeframe
            timeframe_context_by_symbol[symbol] = timeframe_context
            data_notes_by_symbol[symbol] = _unique_texts(
                (
                    *missing_data,
                    *limit_warnings,
                    *_sequence_values(timeframe_context.get("timeframe_limit_warnings")),
                )
            )
    finally:
        if owns_client and hasattr(client, "aclose"):
            await _maybe_await(client.aclose())

    replay_config = ReplayConfig(
        strategy_name=args.strategy,
        modes=tuple(args.modes),
        execution_timeframe=args.execution_timeframe,
        confirmation_timeframe=args.confirmation_timeframe,
        htf_timeframe=args.htf_timeframe,
        bias_timeframe=args.bias_timeframe,
        replay_candles=replay_candles,
        same_candle_policy=args.same_candle_policy,
        max_hold_candles=args.replay_max_hold_candles,
        max_fill_candles=args.replay_max_fill_candles,
        max_setups=args.backtest_max_setups,
        aggressive_toggle=args.aggressive_toggle,
    )
    return StrategyReplayEngine().run(
        candles_by_symbol,
        replay_config,
        timeframe_context_by_symbol=timeframe_context_by_symbol,
        data_notes_by_symbol=data_notes_by_symbol,
    )


async def _fetch_replay_symbol_timeframes(
    *,
    runner: Any,
    client: Any,
    symbol: str,
    args: argparse.Namespace,
    config: ScannerRunConfig,
) -> tuple[Mapping[str, Sequence[Any]], tuple[str, ...], Mapping[str, Any], tuple[str, ...]]:
    limit_warnings: list[str] = []
    primary_limit = _replay_primary_fetch_limit(args, limit_warnings)
    primary_candles = await runner._request_public_api(
        config,
        f"{symbol} replay {args.execution_timeframe} candles",
        lambda: client.get_klines(symbol, args.execution_timeframe, primary_limit),
    )
    candles_by_timeframe, missing_data, timeframe_context = await runner._fetch_strategy_timeframe_candles(
        client=client,
        symbol=symbol,
        config=config,
        primary_candles=primary_candles,
    )
    return candles_by_timeframe, missing_data, timeframe_context, _unique_texts(limit_warnings)


def _replay_primary_fetch_limit(args: argparse.Namespace, warnings: list[str]) -> int:
    requested_limit = args.replay_candles
    if requested_limit > SAFE_REPLAY_CANDLE_LIMIT_MAX:
        warnings.append(
            f"replay_candles limit clamped from {requested_limit} to {SAFE_REPLAY_CANDLE_LIMIT_MAX} "
            f"for safe replay maximum {SAFE_REPLAY_CANDLE_LIMIT_MAX}."
        )
        requested_limit = SAFE_REPLAY_CANDLE_LIMIT_MAX
    if args.fast and requested_limit > FAST_REPLAY_CANDLES:
        warnings.append(f"replay_candles limit clamped from {requested_limit} to {FAST_REPLAY_CANDLES} for fast mode.")
        requested_limit = FAST_REPLAY_CANDLES
    if args.exchange != "binance":
        return requested_limit

    clamped = min(max(requested_limit, BINANCE_KLINE_LIMIT_MIN), BINANCE_KLINE_LIMIT_MAX)
    if clamped != requested_limit:
        warnings.append(
            f"replay_candles limit clamped from {requested_limit} to {clamped} "
            f"for Binance kline limit {BINANCE_KLINE_LIMIT_MIN}-{BINANCE_KLINE_LIMIT_MAX}."
        )
    return clamped


def _clean_error_message(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return text
    return exc.__class__.__name__


def _sequence_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _unique_texts(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in output:
            output.append(cleaned)
    return tuple(output)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _progress_line(symbol_result: ScannerSymbolResult, *, completed: int, total: int) -> str:
    display = build_symbol_display(symbol_result)
    reason = display.short_reason
    if len(reason) > 120:
        reason = f"{reason[:117]}..."
    return (
        f"[{completed}/{total}] {symbol_result.symbol}: "
        f"{display.display_status} | {display.setup_progress_passed}/{display.setup_progress_total} | {reason}"
    )


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


def _json_payload(
    result: ScannerRunResult,
    ranked_results=None,
    *,
    replay_summary: ReplaySummary | None = None,
) -> dict[str, object]:
    payload = result.model_dump(mode="json")
    universe = result.resume_metadata.get("universe") if isinstance(result.resume_metadata, Mapping) else None
    if isinstance(universe, Mapping):
        payload["universe"] = dict(universe)
    ranks_by_index = {}
    if ranked_results is None:
        ranked_results = rank_scan_results(result.results)
    for ranked in ranked_results:
        ranks_by_index[ranked.original_index] = ranked.display_rank
    for index, symbol_result in enumerate(result.results):
        payload["results"][index].update(display_fields(symbol_result, display_rank=ranks_by_index.get(index)))
    if replay_summary is not None:
        payload["replay_result"] = replay_summary.model_dump(mode="json")
        payload.update(backtest_json_payload(replay_summary))
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
            f"Runtime: {_seconds_text(symbol_result.runtime_seconds)}",
            f"Timed out: {_bool_text(symbol_result.timed_out)}",
            f"Timeout status: {symbol_result.timeout_status}",
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
            "Near-miss intelligence:",
            _format_near_miss_intelligence(symbol_result),
            "Setup quality:",
            _format_setup_quality_diagnostics(symbol_result),
            "Strategy diagnostics:",
            _format_strategy_diagnostics(symbol_result),
        )
    )


def _format_run_diagnostics(result: ScannerRunResult) -> str:
    cache_stats = result.cache_stats or {}
    runtime = result.runtime_stats
    retry_events = tuple(result.retry_diagnostics or ())
    retry_lines = [f"Retry events: {len(retry_events)}"]
    for event in retry_events[:10]:
        retry_lines.append(
            (
                f"- {event.get('operation', 'N/A')} attempt {event.get('attempt', 'N/A')}/"
                f"{event.get('attempts', 'N/A')} retry={event.get('will_retry', False)} "
                f"delay={event.get('delay_seconds', 0)}s error={event.get('error_type', 'N/A')}"
            )
        )
    if len(retry_events) > 10:
        retry_lines.append(f"- {len(retry_events) - 10} more retry event(s) omitted.")

    return "\n".join(
        (
            "Run diagnostics:",
            "Runtime diagnostics:",
            f"Total runtime: {_seconds_text(runtime.total_runtime_seconds)}",
            f"Average seconds per symbol: {_seconds_text(runtime.average_seconds_per_symbol)}",
            f"Slowest symbol: {_display(runtime.slowest_symbol)} ({_seconds_text(runtime.slowest_symbol_seconds)})",
            f"Timeout count: {runtime.timeout_count}",
            f"Completed symbols: {runtime.completed_symbols}",
            f"Skipped symbols: {runtime.skipped_symbols}",
            f"Errored symbols: {runtime.errored_symbols}",
            f"Global timeout hit: {_bool_text(runtime.global_timeout_hit)}",
            "Per-symbol timing:",
            *_runtime_symbol_lines(result),
            "Cache diagnostics:",
            f"Enabled: {cache_stats.get('enabled', False)}",
            f"File cache: {cache_stats.get('file_cache_enabled', False)}",
            f"File path: {_display(cache_stats.get('file_path'))}",
            f"Hits: {cache_stats.get('hits', 0)}",
            f"Misses: {cache_stats.get('misses', 0)}",
            f"Expired: {cache_stats.get('expired', 0)}",
            f"Writes: {cache_stats.get('writes', 0)}",
            f"Errors: {cache_stats.get('errors', 0)}",
            "Retry diagnostics:",
            *retry_lines,
        )
    )


def _runtime_symbol_lines(result: ScannerRunResult) -> tuple[str, ...]:
    if not result.results:
        return ("- N/A",)
    return tuple(
        (
            f"- {symbol_result.symbol}: runtime={_seconds_text(symbol_result.runtime_seconds)} "
            f"timeout={symbol_result.timeout_status} status={symbol_result.status.value}"
        )
        for symbol_result in result.results
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


def _seconds_text(value: object) -> str:
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


def _format_near_miss_intelligence(symbol_result: ScannerSymbolResult) -> str:
    intelligence = build_symbol_display(symbol_result).near_miss_intelligence
    if intelligence is None:
        return NA
    return "\n".join(
        (
            f"Primary failed gate: {intelligence.primary_failed_gate}",
            f"Status: {intelligence.watchlist_status}",
            f"Short reason: {intelligence.short_reason}",
            f"Needs next: {_sequence_text(intelligence.next_required_conditions)}",
            f"Activation hint: {intelligence.activation_hint}",
            f"Invalidation hint: {intelligence.invalidation_hint}",
            f"Quality note: {intelligence.quality_note}",
            f"Action: {intelligence.action_label}",
        )
    )


def _format_setup_quality_diagnostics(symbol_result: ScannerSymbolResult) -> str:
    quality = symbol_result.setup_quality
    if not quality.is_evaluated:
        return "N/A"
    return "\n".join(
        (
            f"State: {quality.quality_state.value}",
            f"Grade: {quality.quality_grade.value}",
            f"Quality score: {quality.quality_score}",
            f"Tradeability score: {quality.tradeability_score}",
            f"Profitability edge score: {quality.profitability_edge_score}",
            f"Execution risk score: {quality.execution_risk_score} (lower is better)",
            f"Strongest: {_sequence_text(quality.strongest_factors)}",
            f"Weakest: {_sequence_text(quality.weakest_factors)}",
            f"Action: {quality.action_label}",
            f"Reason: {quality.decision_reason}",
        )
    )


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
        limit_warnings = _sequence_text(_sequence_values(diagnostics.get("timeframe_limit_warnings")))
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
                f"{mode} timeframe limit warnings: {limit_warnings}",
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
