from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

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
from app.analytics.edge_analytics import (  # noqa: E402
    DEFAULT_EDGE_MIN_SAMPLE,
    EdgeAnalyticsReport,
    condition_key_from_diagnostics,
    match_historical_condition,
)
from app.analytics.market_regime import default_market_regime_result, disabled_market_regime_result  # noqa: E402
from app.analytics.performance_memory import (  # noqa: E402
    ConfidenceBucket,
    apply_performance_memory_to_result,
    ingest_replay_summary,
    load_performance_memory,
    reset_performance_memory,
    save_performance_memory,
)
from app.analytics.symbol_health import (  # noqa: E402
    DEFAULT_MAX_TIMEOUT_STRIKES,
    DEFAULT_SYMBOL_COOLDOWN_MINUTES,
    SymbolPriorityPlan,
    build_symbol_priority_plan,
    empty_symbol_priority_plan,
)
from app.analytics.portfolio_selection import (  # noqa: E402
    PortfolioRiskLimits,
    PortfolioSelectionResult,
    build_portfolio_selection_from_scan,
    format_portfolio_selection_summary,
    selected_symbols,
)
from app.data.dtos import NA  # noqa: E402
from app.cache.market_data_cache import MarketDataCache  # noqa: E402
from app.context import BtcDominanceContextService, CoinPaprikaBtcDominanceProvider  # noqa: E402
from app.context.btc_d import (  # noqa: E402
    DEFAULT_BTC_D_FRESH_SECONDS,
    DEFAULT_BTC_D_MAX_STALE_SECONDS,
)
from app.microstructure.liquidation_service import LiquidationFlowService  # noqa: E402
from app.microstructure.service import MicrostructureFlowService  # noqa: E402
from app.command_center import (  # noqa: E402
    build_command_center_payload,
    build_minimum_rr_audit,
    build_minimum_rr_policy_payload,
    format_command_center_report,
    format_command_center_summary,
    format_portfolio_command_summary,
    format_top_setup_spotlight,
    format_watchlist_export,
)
from app.core.config import Settings  # noqa: E402
from app.core.minimum_rr import (  # noqa: E402
    DEFAULT_CONFIGURED_MINIMUM_RR,
    MinimumRRConfigurationError,
    validate_configured_minimum_rr,
)
from app.alerts.telegram_lifecycle import (  # noqa: E402
    TelegramLifecycleDeliveryService,
    TelegramLifecycleDeliverySummary,
)
from app.alerts.telegram_sender import resolve_public_signal_destination  # noqa: E402
from app.formatters.scanner_console import ScannerConsolePresenter  # noqa: E402
from app.formatters.scanner_display import (  # noqa: E402
    DEFAULT_MAX_DISPLAY_RESULTS,
    DisplayBucket,
    build_symbol_display,
    display_fields,
    filter_ranked_results,
    format_pullback_intelligence_block,
    format_scan_dashboard,
    format_symbol_card,
    format_symbol_compact_line,
    rank_scan_results,
    representative_strategy_diagnostics,
)
from app.formatters.telegram_formatter import format_telegram_strategy_output  # noqa: E402
from app.telegram_admin import TelegramAdminConfig, route_admin_scan_report  # noqa: E402
from app.lifecycle.models import lifecycle_monitoring_priority  # noqa: E402
from app.lifecycle.service import (  # noqa: E402
    SetupLifecycleService,
    apply_lifecycle_to_run_result,
    active_lifecycle_symbols,
)
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository  # noqa: E402
from app.pipeline.scanner_runner import (  # noqa: E402
    DEFAULT_CONFIRMATION_TIMEFRAME,
    DEFAULT_STRUCTURE_TIMEFRAME,
    BINANCE_KLINE_LIMIT_MAX,
    BINANCE_KLINE_LIMIT_MIN,
    DEFAULT_REPLAY_CANDLES,
    DEFAULT_REQUEST_TIMEOUT_SEC,
    DEFAULT_SYMBOL_TIMEOUT_SEC,
    FAST_CANDLE_LIMIT,
    FAST_REPLAY_CANDLES,
    SAFE_REPLAY_CANDLE_LIMIT_MAX,
    ScannerPipelineStatus,
    ScannerProcessMemoryStats,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerRuntimeStats,
    ScannerRunner,
    ScannerSymbolResult,
)
from app.research import (  # noqa: E402
    RESEARCH_QUERIES,
    ResearchDatabaseMissing,
    ResearchFilters,
    build_research_report,
    format_research_report,
)
from app.storage import (  # noqa: E402
    DEFAULT_DATABASE_PATH,
    StorageError,
    WatchIterationMetadata,
    export_history_payload,
    format_history_table,
    list_scan_history,
    load_symbol_health_records,
    store_scan_result,
    update_symbol_health_for_result,
)
from app.universe.symbol_universe import (  # noqa: E402
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
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
from app.watch_mode import (  # noqa: E402
    DEFAULT_LATEST_RUN_PATH,
    DEFAULT_WATCH_STATE_PATH,
    WatchActivation,
    WatchIterationSummary,
    WatchModeError,
    append_watch_output,
    build_watch_activation_alert_manifest,
    build_watch_iteration_summary,
    deliver_watch_activation_alert,
    format_watch_activation_alert,
    format_watch_iteration_summary,
    load_run_payload,
    load_symbols_from_run,
    load_watch_state,
    save_watch_state,
    seed_watch_state_from_run_payload,
    should_trigger_activation_alert,
    state_watch_symbols,
    update_watch_state_for_result,
)
from app.watch_iteration import (  # noqa: E402
    not_run_symbol_result as _not_run_symbol_result,
    queued_symbol_outcome_counts as _queued_symbol_outcome_counts,
    queued_symbol_outcomes as _queued_symbol_outcomes,
    scanner_phase_status as _scanner_phase_status,
    telegram_outbox_phase_status as _telegram_outbox_phase_status,
    telegram_outbox_status_summary as _telegram_outbox_status_summary,
    watch_phase_error as _watch_phase_error,
)
from app.watch_supervisor import (  # noqa: E402
    WatchFailureDisposition,
    WatchIterationStatus,
    classify_watch_exception,
    failure_backoff_seconds,
    schedule_after_iteration,
)


DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
LATEST_RUN_PATH = DEFAULT_LATEST_RUN_PATH
WATCH_STATE_PATH = DEFAULT_WATCH_STATE_PATH
PERFORMANCE_MEMORY_PATH = PROJECT_ROOT / "scan_runs" / "performance_memory.json"
SCAN_RUN_MANIFEST_PATH = PROJECT_ROOT / "scan_runs" / "scan_run_manifest.jsonl"
NIGHTLY_SCAN_HISTORY_PATH = PROJECT_ROOT / "scan_runs" / "nightly_scan_history.json"
ADMIN_DRAFTS_DIR = PROJECT_ROOT / "scan_runs" / "admin_drafts"


@dataclass(frozen=True)
class WatchlistResolution:
    symbols: tuple[str, ...]
    source_label: str
    universe: SymbolUniverse
    explicit_excluded_symbols: tuple[str, ...] = ()
    pre_cap_symbols_count: int | None = None
    queue_cap_applied: bool = False
    lifecycle_priority_promoted_symbols: tuple[str, ...] = ()
    lifecycle_priority_added_symbols: tuple[str, ...] = ()
    lifecycle_priority_dropped_symbols: tuple[str, ...] = ()

    active_lifecycle_symbols: tuple[str, ...] = ()
    active_lifecycle_over_cap_count: int = 0
    lifecycle_capacity_displaced_symbols: tuple[str, ...] = ()
    membership_boundary_ignored_symbols: tuple[str, ...] = ()
    lifecycle_membership_ignored_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResumeState:
    results_by_symbol: dict[str, ScannerSymbolResult]
    skipped_symbols: tuple[str, ...]
    loaded_symbols: tuple[str, ...]


@dataclass(frozen=True)
class WatchScanExecution:
    result: ScannerRunResult
    ranked_results: tuple[Any, ...]
    portfolio_selection: PortfolioSelectionResult | None
    symbol_priority_plan: SymbolPriorityPlan
    queued_symbols: tuple[str, ...]
    storage_run_id: str | None = None
    phase_statuses: dict[str, str] = field(default_factory=dict)
    recoverable_errors: tuple[str, ...] = ()
    telegram_outbox_status: dict[str, int] = field(default_factory=dict)
    telegram_delivery_summary: TelegramLifecycleDeliverySummary | None = None


@dataclass(frozen=True)
class CommandPreset:
    name: str
    modes: tuple[str, ...]
    htf_timeframe: str
    bias_timeframe: str
    structure_timeframe: str
    execution_timeframe: str
    confirmation_timeframe: str
    min_score_for_idea: str
    min_rr: Decimal
    diagnostics_level: str
    display: str
    rank_results: bool = True
    portfolio_select: bool = False
    continue_watch: bool = False
    candle_limit: int | None = None
    fast: bool = False
    request_timeout_sec: float | None = None
    symbol_timeout_sec: float | None = None
    scan_timeout_sec: float | None = None
    max_display_results: int | None = None


COMMAND_PRESETS: dict[str, CommandPreset] = {
    "daily": CommandPreset(
        name="daily",
        modes=("challenge", "swing", "scalp"),
        htf_timeframe="2d",
        bias_timeframe="12h",
        structure_timeframe=DEFAULT_STRUCTURE_TIMEFRAME,
        execution_timeframe="15m",
        confirmation_timeframe=DEFAULT_CONFIRMATION_TIMEFRAME,
        min_score_for_idea="80",
        min_rr=Decimal("2.5"),
        diagnostics_level="normal",
        display="normal",
        rank_results=True,
        portfolio_select=True,
        continue_watch=True,
        max_display_results=10,
    ),
    "swing": CommandPreset(
        name="swing",
        modes=("swing",),
        htf_timeframe="2d",
        bias_timeframe="12h",
        structure_timeframe=DEFAULT_STRUCTURE_TIMEFRAME,
        execution_timeframe="15m",
        confirmation_timeframe=DEFAULT_CONFIRMATION_TIMEFRAME,
        min_score_for_idea="80",
        min_rr=Decimal("2.5"),
        diagnostics_level="normal",
        display="normal",
        rank_results=True,
        portfolio_select=True,
        continue_watch=True,
        max_display_results=10,
    ),
    "challenge": CommandPreset(
        name="challenge",
        modes=("challenge",),
        htf_timeframe="2d",
        bias_timeframe="12h",
        structure_timeframe=DEFAULT_STRUCTURE_TIMEFRAME,
        execution_timeframe="15m",
        confirmation_timeframe=DEFAULT_CONFIRMATION_TIMEFRAME,
        min_score_for_idea="85",
        min_rr=Decimal("3.0"),
        diagnostics_level="normal",
        display="normal",
        rank_results=True,
        portfolio_select=True,
        continue_watch=True,
        max_display_results=10,
    ),
    "scalp": CommandPreset(
        name="scalp",
        modes=("scalp",),
        htf_timeframe="12h",
        bias_timeframe="4h",
        structure_timeframe=DEFAULT_STRUCTURE_TIMEFRAME,
        execution_timeframe="15m",
        confirmation_timeframe=DEFAULT_CONFIRMATION_TIMEFRAME,
        min_score_for_idea="80",
        min_rr=Decimal("2.5"),
        diagnostics_level="summary",
        display="compact",
        rank_results=True,
        portfolio_select=False,
        continue_watch=True,
        candle_limit=180,
        fast=True,
        request_timeout_sec=5.0,
        symbol_timeout_sec=20.0,
        scan_timeout_sec=90.0,
        max_display_results=8,
    ),
}


def _non_negative_decimal_arg(value: str) -> Decimal:
    try:
        decimal = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a decimal number") from exc
    if not decimal.is_finite() or decimal < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return decimal


def _minimum_rr_arg(value: str) -> Decimal:
    try:
        return validate_configured_minimum_rr(value)
    except MinimumRRConfigurationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive_float_arg(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _bool_arg(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def _explicit_cli_options(tokens: Sequence[str]) -> set[str]:
    aliases = {
        "--symbols": "symbols",
        "--universe": "universe",
        "--preset": "preset",
        "--command-preset": "command_preset",
        "--command": "command_preset",
        "--modes": "modes",
        "--htf-timeframe": "htf_timeframe",
        "--bias-timeframe": "bias_timeframe",
        "--structure-timeframe": "structure_timeframe",
        "--execution-timeframe": "execution_timeframe",
        "--confirmation-timeframe": "confirmation_timeframe",
        "--min-score-for-idea": "min_score_for_idea",
        "--min-rr": "min_rr",
        "--diagnostics-level": "diagnostics_level",
        "--display": "display",
        "--rank-results": "rank_results",
        "--no-rank-results": "rank_results",
        "--portfolio-select": "portfolio_select",
        "--no-portfolio-select": "portfolio_select",
        "--continue-watch": "continue_watch",
        "--no-continue-watch": "continue_watch",
        "--watch-only-near-misses": "watch_only_near_misses",
        "--fast": "fast",
        "--no-fast": "fast",
        "--candle-limit": "candle_limit",
        "--request-timeout-sec": "request_timeout_sec",
        "--symbol-timeout-sec": "symbol_timeout_sec",
        "--scan-timeout-sec": "scan_timeout_sec",
        "--max-scan-seconds": "scan_timeout_sec",
        "--max-display-results": "max_display_results",
        "--max-selected-setups": "max_selected_setups",
        "--max-portfolio-risk-pct": "max_portfolio_risk_pct",
        "--max-beta-group-risk-pct": "max_beta_group_risk_pct",
        "--allow-correlated-setups": "allow_correlated_setups",
        "--market-regime": "market_regime",
        "--disable-regime-filter": "market_regime",
        "--regime-risk-mode": "regime_risk_mode",
        "--regime-strictness": "regime_strictness",
        "--show-regime-details": "show_regime_details",
        "--show-pullback-details": "show_pullback_details",
        "--performance-memory": "performance_memory",
        "--disable-performance-memory": "performance_memory",
        "--reset-performance-memory": "reset_performance_memory",
        "--min-memory-confidence": "min_memory_confidence",
        "--store-scan": "store_scan",
        "--database-path": "database_path",
        "--telegram-manual-signals": "telegram_manual_signals",
        "--telegram-signals": "telegram_manual_signals",
        "--no-telegram-manual-signals": "telegram_manual_signals",
        "--no-telegram-signals": "telegram_manual_signals",
        "--lifecycle": "lifecycle",
        "--disable-lifecycle": "lifecycle",
        "--show-lifecycle": "show_lifecycle",
        "--reset-lifecycle": "reset_lifecycle",
        "--show-history": "show_history",
        "--history-limit": "history_limit",
        "--export-history-json": "export_history_json",
        "--research": "research",
        "--research-query": "research_query",
        "--research-limit": "research_limit",
        "--research-symbol": "research_symbol",
        "--research-mode": "research_mode",
        "--research-regime": "research_regime",
        "--research-output-json": "research_output_json",
        "--lifecycle-stale-hours": "lifecycle_stale_hours",
        "--adaptive-symbol-priority": "adaptive_symbol_priority",
        "--no-adaptive-symbol-priority": "adaptive_symbol_priority",
        "--symbol-cooldown-minutes": "symbol_cooldown_minutes",
        "--max-timeout-strikes": "max_timeout_strikes",
        "--show-symbol-health": "show_symbol_health",
    }
    explicit: set[str] = set()
    for token in tokens:
        option = token.split("=", 1)[0]
        field = aliases.get(option)
        if field is not None:
            explicit.add(field)
    return explicit


def _normalize_minimum_rr_cli_tokens(tokens: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        if (
            tokens[index] == "--min-rr"
            and index + 1 < len(tokens)
            and tokens[index + 1].lower() in {"-inf", "-infinity"}
        ):
            normalized.append(f"--min-rr={tokens[index + 1]}")
            index += 2
            continue
        normalized.append(tokens[index])
        index += 1
    return normalized


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    tokens = _normalize_minimum_rr_cli_tokens(sys.argv[1:] if argv is None else argv)
    explicit_options = _explicit_cli_options(tokens)
    symbols_explicit = any(token == "--symbols" or token.startswith("--symbols=") for token in tokens)
    diagnostics_level_explicit = any(
        token == "--diagnostics-level" or token.startswith("--diagnostics-level=") for token in tokens
    )
    display_explicit = any(token == "--display" or token.startswith("--display=") for token in tokens)
    parser = argparse.ArgumentParser(description="Run the Candle Craft dry-run scanner pipeline.")
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--command-preset", "--command", choices=sorted(COMMAND_PRESETS))
    parser.add_argument("--list-command-presets", action="store_true")
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
    parser.add_argument("--min-rr", type=_minimum_rr_arg, default=DEFAULT_CONFIGURED_MINIMUM_RR)
    parser.add_argument("--strategy", choices=["liquidity_grab_pullback"], default="liquidity_grab_pullback")
    parser.add_argument("--modes", nargs="+", choices=["challenge", "swing", "scalp"], default=["challenge", "swing", "scalp"])
    parser.add_argument("--htf-timeframe", default="2d")
    parser.add_argument("--bias-timeframe", default="12h")
    parser.add_argument("--structure-timeframe", default=DEFAULT_STRUCTURE_TIMEFRAME)
    parser.add_argument("--execution-timeframe", default="15m")
    parser.add_argument("--confirmation-timeframe", default=DEFAULT_CONFIRMATION_TIMEFRAME)
    parser.add_argument("--aggressive-toggle", action="store_true")
    parser.add_argument("--replay", "--backtest", dest="replay", action="store_true")
    parser.add_argument("--replay-candles", "--backtest-candles", dest="replay_candles", type=int, default=DEFAULT_REPLAY_CANDLES)
    parser.add_argument("--same-candle-policy", choices=["conservative", "optimistic"], default="conservative")
    parser.add_argument("--replay-max-hold-candles", type=int)
    parser.add_argument("--replay-max-fill-candles", type=int)
    parser.add_argument("--backtest-max-setups", type=int)
    parser.add_argument("--backtest-output-json", type=Path)
    parser.add_argument("--backtest-summary-only", action="store_true")
    parser.add_argument("--edge-analytics", action="store_true")
    parser.add_argument("--edge-min-sample", type=int, default=DEFAULT_EDGE_MIN_SAMPLE)
    parser.add_argument("--edge-export-json", type=Path)
    parser.add_argument("--request-timeout-sec", type=_positive_float_arg, default=DEFAULT_REQUEST_TIMEOUT_SEC)
    parser.add_argument("--symbol-timeout-sec", type=_positive_float_arg, default=DEFAULT_SYMBOL_TIMEOUT_SEC)
    parser.add_argument("--scan-timeout-sec", "--max-scan-seconds", dest="scan_timeout_sec", type=_positive_float_arg)
    parser.add_argument("--adaptive-symbol-priority", dest="adaptive_symbol_priority", action="store_true", default=None)
    parser.add_argument("--no-adaptive-symbol-priority", dest="adaptive_symbol_priority", action="store_false")
    parser.add_argument(
        "--symbol-cooldown-minutes",
        type=_positive_float_arg,
        default=DEFAULT_SYMBOL_COOLDOWN_MINUTES,
    )
    parser.add_argument("--max-timeout-strikes", type=int, default=DEFAULT_MAX_TIMEOUT_STRIKES)
    parser.add_argument("--show-symbol-health", action="store_true")
    parser.add_argument("--fast", dest="fast", action="store_true", default=False)
    parser.add_argument("--no-fast", dest="fast", action="store_false")
    parser.add_argument("--show-strategy-output", action="store_true")
    parser.add_argument("--telegram-format", action="store_true")
    parser.add_argument("--diagnostics-level", choices=["summary", "normal", "full"], default="normal")
    parser.add_argument("--display", choices=["compact", "normal", "full"], default="normal")
    parser.add_argument("--rank-results", dest="rank_results", action="store_true", default=True)
    parser.add_argument("--no-rank-results", dest="rank_results", action="store_false")
    parser.add_argument("--show-no-setups", action="store_true")
    parser.add_argument("--max-display-results", type=int, default=DEFAULT_MAX_DISPLAY_RESULTS)
    parser.add_argument("--bucket-filter", nargs="+")
    parser.add_argument("--portfolio-select", dest="portfolio_select", action="store_true", default=False)
    parser.add_argument("--no-portfolio-select", dest="portfolio_select", action="store_false")
    parser.add_argument("--max-selected-setups", type=int, default=3)
    parser.add_argument("--max-portfolio-risk-pct", type=_non_negative_decimal_arg, default=Decimal("3"))
    parser.add_argument("--max-beta-group-risk-pct", type=_non_negative_decimal_arg, default=Decimal("1.5"))
    parser.add_argument("--allow-correlated-setups", action="store_true")
    parser.add_argument("--market-regime", dest="market_regime", action="store_true", default=True)
    parser.add_argument("--disable-regime-filter", dest="market_regime", action="store_false")
    parser.add_argument("--regime-risk-mode", choices=["conservative", "balanced", "aggressive"], default="balanced")
    parser.add_argument("--regime-strictness", choices=["low", "normal", "high"], default="normal")
    parser.add_argument("--show-regime-details", action="store_true")
    parser.add_argument("--performance-memory", dest="performance_memory", action="store_true", default=None)
    parser.add_argument("--disable-performance-memory", dest="performance_memory", action="store_false")
    parser.add_argument("--reset-performance-memory", action="store_true")
    parser.add_argument(
        "--min-memory-confidence",
        choices=[bucket.value for bucket in ConfidenceBucket],
        default=ConfidenceBucket.LOW.value,
    )
    parser.add_argument("--continue-watch", dest="continue_watch", action="store_true", default=False)
    parser.add_argument("--no-continue-watch", dest="continue_watch", action="store_false")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--watch-interval-sec", type=_positive_float_arg, default=60.0)
    parser.add_argument("--watch-max-iterations", type=int)
    parser.add_argument("--watch-symbols-from-latest-run", action="store_true")
    parser.add_argument("--watch-only-near-misses", action="store_true")
    parser.add_argument("--watch-output-file", type=Path)
    parser.add_argument(
        "--console-mode",
        choices=["compact", "verbose"],
        default=None,
        help="Watch console output: compact summary or verbose diagnostics. Watch defaults to compact.",
    )
    parser.add_argument("--telegram-live-alerts", nargs="?", const=True, default=False, type=_bool_arg)
    parser.add_argument(
        "--show-near-miss-plan",
        action="store_true",
        help="Print the near-miss plan block even when compact display is selected.",
    )
    parser.add_argument(
        "--show-pullback-details",
        action="store_true",
        help="Print pullback intelligence diagnostics for visible non-valid setup cards.",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--export-report", nargs="?", const=Path("scan_runs/latest_report.txt"), type=Path)
    parser.add_argument("--export-json", nargs="?", const=Path("scan_runs/latest_command_center.json"), type=Path)
    parser.add_argument("--export-watchlist", nargs="?", const=Path("scan_runs/latest_watchlist.txt"), type=Path)
    parser.add_argument("--cache", dest="cache_enabled", action="store_true", default=True)
    parser.add_argument("--no-cache", dest="cache_enabled", action="store_false")
    parser.add_argument("--cache-ttl-seconds", type=int)
    parser.add_argument("--cache-file", type=Path)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--save-run", nargs="?", const=Path("scan_runs/latest_scan.json"), type=Path)
    parser.add_argument("--store-scan", action="store_true")
    parser.add_argument("--database-path", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--telegram-manual-signals",
        "--telegram-signals",
        dest="telegram_manual_signals",
        action="store_true",
        default=None,
    )
    parser.add_argument(
        "--no-telegram-manual-signals",
        "--no-telegram-signals",
        dest="telegram_manual_signals",
        action="store_false",
    )
    parser.add_argument("--lifecycle", dest="lifecycle", action="store_true", default=None)
    parser.add_argument("--disable-lifecycle", dest="lifecycle", action="store_false")
    parser.add_argument("--show-lifecycle", action="store_true")
    parser.add_argument("--reset-lifecycle", action="store_true")
    parser.add_argument("--show-history", action="store_true")
    parser.add_argument("--history-limit", type=int, default=10)
    parser.add_argument("--export-history-json", type=Path)
    parser.add_argument("--research", action="store_true")
    parser.add_argument("--research-query", choices=RESEARCH_QUERIES, default="summary")
    parser.add_argument("--research-limit", type=int, default=10)
    parser.add_argument("--research-symbol")
    parser.add_argument("--research-mode", choices=["challenge", "swing", "scalp"])
    parser.add_argument("--research-regime")
    parser.add_argument("--lifecycle-stale-hours", type=_positive_float_arg, default=24.0)
    parser.add_argument(
        "--research-output-json",
        nargs="?",
        const=Path("scan_runs") / "research_report.json",
        type=Path,
    )
    parser.add_argument("--no-resume-skip", action="store_true")
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args(tokens)
    _apply_command_preset(args, explicit_options)
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
    if args.research_limit < 1:
        parser.error("--research-limit must be at least 1.")
    if args.edge_min_sample < 1:
        parser.error("--edge-min-sample must be at least 1.")
    if args.max_selected_setups < 1:
        parser.error("--max-selected-setups must be at least 1.")
    if args.watch_max_iterations is not None and args.watch_max_iterations < 1:
        parser.error("--watch-max-iterations must be at least 1.")
    if args.history_limit < 1:
        parser.error("--history-limit must be at least 1.")
    if args.max_timeout_strikes < 1:
        parser.error("--max-timeout-strikes must be at least 1.")
    if args.backtest_output_json is not None:
        args.replay = True
    if args.edge_export_json is not None:
        args.edge_analytics = True
    if args.edge_analytics:
        args.replay = True
    args.symbols_explicit = symbols_explicit
    args.diagnostics_level_explicit = diagnostics_level_explicit
    args.display_explicit = display_explicit
    return args


def _apply_command_preset(args: argparse.Namespace, explicit_options: set[str]) -> None:
    if args.command_preset is None:
        return
    preset = COMMAND_PRESETS[args.command_preset]
    preset_values: dict[str, object] = {
        "modes": list(preset.modes),
        "htf_timeframe": preset.htf_timeframe,
        "bias_timeframe": preset.bias_timeframe,
        "structure_timeframe": preset.structure_timeframe,
        "execution_timeframe": preset.execution_timeframe,
        "confirmation_timeframe": preset.confirmation_timeframe,
        "min_score_for_idea": preset.min_score_for_idea,
        "min_rr": preset.min_rr,
        "diagnostics_level": preset.diagnostics_level,
        "display": preset.display,
        "rank_results": preset.rank_results,
        "portfolio_select": preset.portfolio_select,
        "continue_watch": preset.continue_watch,
        "fast": preset.fast,
    }
    optional_values = {
        "candle_limit": preset.candle_limit,
        "request_timeout_sec": preset.request_timeout_sec,
        "symbol_timeout_sec": preset.symbol_timeout_sec,
        "scan_timeout_sec": preset.scan_timeout_sec,
        "max_display_results": preset.max_display_results,
    }
    for field, value in optional_values.items():
        if value is not None:
            preset_values[field] = value
    for field, value in preset_values.items():
        if field not in explicit_options:
            setattr(args, field, value)


def _watch_console_mode(args: argparse.Namespace) -> str:
    if args.console_mode is not None:
        return args.console_mode
    if args.verbose:
        return "verbose"
    return "compact"


async def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.reset_lifecycle:
        _reset_lifecycle_state(args)
    if args.list_command_presets:
        print(_format_available_command_presets())
        return
    if args.list_presets:
        print(_format_available_presets())
        return
    if args.research:
        _handle_research_command(args)
        return
    if args.show_history or args.export_history_json is not None:
        _handle_history_command(args)
        return

    runtime_settings = Settings()
    watchlist = await _resolve_watchlist_for_args(args)
    watchlist = _watchlist_with_lifecycle_priority(args, watchlist)
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
        min_rr=args.min_rr,
        verbose=diagnostics_level == "full",
        strategy_name=args.strategy,
        strategy_modes=args.modes,
        enable_strategy_output=True,
        include_formatted_strategy_output=True,
        aggressive_toggle=args.aggressive_toggle,
        htf_timeframe=args.htf_timeframe,
        bias_timeframe=args.bias_timeframe,
        structure_timeframe=args.structure_timeframe,
        execution_timeframe=args.execution_timeframe,
        confirmation_timeframe=args.confirmation_timeframe,
        cache_enabled=args.cache_enabled,
        cache_ttl_seconds=args.cache_ttl_seconds,
        cache_file=args.cache_file,
        request_timeout_sec=args.request_timeout_sec,
        symbol_timeout_sec=args.symbol_timeout_sec,
        scan_timeout_sec=args.scan_timeout_sec,
        fast_mode=args.fast,
        market_regime_enabled=args.market_regime,
        regime_risk_mode=args.regime_risk_mode,
        regime_strictness=args.regime_strictness,
        global_context_enabled=runtime_settings.global_context_enabled,
        btc_context_enabled=runtime_settings.btc_context_enabled,
        btc_d_context_enabled=runtime_settings.btc_d_context_enabled,
        btc_d_cache_ttl_sec=runtime_settings.btc_d_cache_ttl_sec,
        btc_d_request_timeout_sec=runtime_settings.btc_d_request_timeout_sec,
        microstructure_flow_enabled=runtime_settings.microstructure_flow_enabled,
        microstructure_flow_stale_sec=runtime_settings.microstructure_flow_stale_sec,
        microstructure_flow_max_symbols=runtime_settings.microstructure_flow_max_symbols,
        liquidation_flow_enabled=runtime_settings.liquidation_flow_enabled,
        liquidation_flow_stale_sec=runtime_settings.liquidation_flow_stale_sec,
        liquidation_flow_max_symbols=runtime_settings.liquidation_flow_max_symbols,
    )
    symbol_priority_plan = _symbol_priority_plan_for_watchlist(args, watchlist)

    if args.watch:
        await _run_watch_mode(
            args,
            watchlist=watchlist,
            config=config,
            diagnostics_level=diagnostics_level,
            display_mode=display_mode,
            effective_candle_limit=effective_candle_limit,
            command_used=_command_used(argv),
        )
        return

    queued_symbols = _queued_symbols_for_scan(args, watchlist, symbol_priority_plan)
    print(_format_universe_header(watchlist.universe))
    print(f"Watchlist: {watchlist.source_label}")
    print(f"Symbols queued: {len(queued_symbols)}")
    print(f"Timeframe hierarchy: context={args.htf_timeframe} -> bias={args.bias_timeframe} -> structure={args.structure_timeframe} -> execution={args.execution_timeframe} -> confirmation={args.confirmation_timeframe}")
    _print_symbol_queue_diagnostics(args, watchlist, symbol_priority_plan, queued_symbols)
    for warning in _startup_warnings(args, effective_candle_limit):
        print(f"Warning: {warning}")
    print("")

    resume_state = _load_resume_state(args.resume_from, watchlist.symbols, skip_completed=not args.no_resume_skip)
    resume_state = _resume_state_with_active_lifecycle_retry(resume_state, watchlist.active_lifecycle_symbols)
    if args.progress and resume_state.skipped_symbols:
        print(f"Resume: skipped {len(resume_state.skipped_symbols)} completed symbol(s).")

    cache = (
        MarketDataCache(enabled=True, ttl_seconds=args.cache_ttl_seconds, file_path=args.cache_file)
        if args.cache_enabled
        else None
    )
    latest_results_by_symbol = dict(resume_state.results_by_symbol)
    symbols_to_scan = tuple(symbol for symbol in queued_symbols if symbol not in resume_state.skipped_symbols)
    symbol_queue_diagnostics = _symbol_queue_diagnostics(args, watchlist, symbol_priority_plan, queued_symbols)
    scan_config = (
        ScannerRunConfig.model_validate({**config.model_dump(), "symbols": list(symbols_to_scan)})
        if symbols_to_scan
        else config
    )
    scan_run_id = uuid4().hex
    resume_metadata = {
        **_resume_metadata(
            args,
            watchlist.symbols,
            resume_state,
            symbols_to_scan,
            watchlist.universe,
            symbol_queue_diagnostics=symbol_queue_diagnostics,
        ),
        "run_id": scan_run_id,
        "scan_run_id": scan_run_id,
    }

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
                market_regime=None,
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
            market_regime=scan_result.market_regime,
            global_context=scan_result.global_context,
        )
    else:
        result = _combined_run_result(
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
            market_regime=None,
        )

    if args.save_run is not None:
        _write_run_json(args.save_run, result)

    replay_summary: ReplaySummary | None = None
    if args.replay:
        replay_summary = await _run_replay(args, watchlist, config, cache)
    if args.edge_analytics and replay_summary is not None:
        result = _apply_edge_analytics_to_result(result, replay_summary.edge_analytics)
        if args.save_run is not None:
            _write_run_json(args.save_run, result, replay_summary=replay_summary)

    memory_enabled = _performance_memory_enabled(args)
    if args.reset_performance_memory:
        reset_performance_memory(PERFORMANCE_MEMORY_PATH)
    if memory_enabled:
        memory_store = load_performance_memory(PERFORMANCE_MEMORY_PATH)
        if replay_summary is not None:
            ingestion = ingest_replay_summary(memory_store, replay_summary)
            memory_store = ingestion.store
            save_performance_memory(memory_store, PERFORMANCE_MEMORY_PATH)
        elif not PERFORMANCE_MEMORY_PATH.exists():
            save_performance_memory(memory_store, PERFORMANCE_MEMORY_PATH)
        result = apply_performance_memory_to_result(
            result,
            memory_store,
            enabled=True,
            min_confidence=args.min_memory_confidence,
        )
        if args.save_run is not None:
            _write_run_json(args.save_run, result, replay_summary=replay_summary)
    elif args.performance_memory is False:
        result = apply_performance_memory_to_result(
            result,
            load_performance_memory(PERFORMANCE_MEMORY_PATH),
            enabled=False,
            min_confidence=args.min_memory_confidence,
        )

    lifecycle_scan_run_id = scan_run_id if _lifecycle_scan_run_id_enabled(args) else None
    result = _apply_lifecycle_if_enabled(args, result, scan_run_id=lifecycle_scan_run_id)
    await _deliver_telegram_manual_signals_if_enabled(args, result, scan_run_id=scan_run_id)
    result = _apply_symbol_health_if_enabled(args, result, symbol_priority_plan)

    bucket_filter = _parse_bucket_filter(args.bucket_filter)
    ranked_results = rank_scan_results(result.results, rank_results=args.rank_results)
    portfolio_selection = _portfolio_selection_for_result(args, result) if args.portfolio_select else None
    if portfolio_selection is not None:
        ranked_results = _ranked_results_with_selected_first(ranked_results, portfolio_selection)
    visible_results = filter_ranked_results(
        ranked_results,
        show_no_setups=args.show_no_setups,
        bucket_filter=bucket_filter,
        max_display_results=args.max_display_results,
    )
    continued_watch_symbols = tuple(getattr(args, "continued_watch_symbols", ()))
    promoted_watch_symbols = _update_continue_watch_state(args, result) if args.continue_watch else ()
    export_watch_symbols = promoted_watch_symbols or _watch_candidate_symbols(result)
    replay_warnings = _replay_warning_lines(replay_summary)
    command_center_payload = build_command_center_payload(
        result,
        ranked_results=ranked_results,
        portfolio_selection=portfolio_selection,
        promoted_watch_symbols=promoted_watch_symbols,
        continued_watch_symbols=continued_watch_symbols,
        command_preset=args.command_preset,
        min_rr=args.min_rr,
        replay_warnings=replay_warnings,
    )
    if args.output_json is not None:
        _write_run_json(
            args.output_json,
            result,
            ranked_results=ranked_results,
            replay_summary=replay_summary,
            portfolio_selection=portfolio_selection,
        )
    if args.save_run is not None:
        _write_run_json(
            args.save_run,
            result,
            ranked_results=ranked_results,
            replay_summary=replay_summary,
            portfolio_selection=portfolio_selection,
        )
    if args.backtest_output_json is not None and replay_summary is not None:
        _write_backtest_json(args.backtest_output_json, replay_summary)
    if args.edge_export_json is not None and replay_summary is not None:
        _write_edge_json(args.edge_export_json, replay_summary.edge_analytics)
    if args.export_json is not None:
        _write_export_json(
            args.export_json,
            result,
            ranked_results=ranked_results,
            replay_summary=replay_summary,
            portfolio_selection=portfolio_selection,
            command_center=command_center_payload,
        )
    if args.export_report is not None:
        _write_text_file(
            args.export_report,
            format_command_center_report(
                result,
                ranked_results=ranked_results,
                portfolio_selection=portfolio_selection,
                promoted_watch_symbols=promoted_watch_symbols,
                continued_watch_symbols=continued_watch_symbols,
                command_preset=args.command_preset,
                min_rr=args.min_rr,
                replay_warnings=replay_warnings,
            ),
        )
    if args.export_watchlist is not None:
        _write_text_file(
            args.export_watchlist,
            format_watchlist_export(
                result,
                promoted_watch_symbols=export_watch_symbols,
                continued_watch_symbols=continued_watch_symbols,
            ),
        )
    stored_scan_run_id = scan_run_id
    if args.store_scan:
        raw_payload = _json_payload(
            result,
            ranked_results=ranked_results,
            replay_summary=replay_summary,
            portfolio_selection=portfolio_selection,
        )
        try:
            stored_scan_run_id = store_scan_result(
                args.database_path,
                result,
                ranked_results=ranked_results,
                replay_summary=replay_summary,
                portfolio_selection=portfolio_selection,
                command_preset=args.command_preset,
                command_used=_command_used(argv),
                raw_payload=raw_payload,
                run_id=stored_scan_run_id,
            )
        except StorageError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Stored scan run: {stored_scan_run_id}")
        print(f"Database: {args.database_path}")
        print("")
    manifest_row = _append_scan_run_manifest(
        result,
        watchlist=watchlist,
        ranked_results=ranked_results,
        manifest_path=SCAN_RUN_MANIFEST_PATH,
        nightly_history_path=NIGHTLY_SCAN_HISTORY_PATH,
        run_id=stored_scan_run_id,
        output_scan_path=args.output_json,
        latest_scan_path=args.save_run,
    )
    await _route_admin_report(
        result,
        ranked_results=ranked_results,
        manifest_row=manifest_row,
    )

    print(format_scan_dashboard(result, ranked_results=ranked_results, visible_results=visible_results))
    if args.show_regime_details:
        print("")
        print(_format_regime_details(result))
    print("")
    print(
        format_command_center_summary(
            result,
            ranked_results=ranked_results,
            portfolio_selection=portfolio_selection,
            promoted_watch_symbols=promoted_watch_symbols,
            continued_watch_symbols=continued_watch_symbols,
            command_preset=args.command_preset,
            min_rr=args.min_rr,
            replay_warnings=replay_warnings,
        )
    )
    top_setup_result = _top_setup_result(ranked_results)
    if top_setup_result is not None:
        print("")
        print(format_top_setup_spotlight(top_setup_result))
    if portfolio_selection is not None:
        print("")
        print(format_portfolio_command_summary(portfolio_selection))
    if portfolio_selection is not None:
        print("")
        print(format_portfolio_selection_summary(portfolio_selection))
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
            elif args.show_pullback_details and ranked.display.display_bucket != "valid":
                print("")
                print(format_pullback_intelligence_block(symbol_result))
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
            elif args.show_pullback_details and ranked.display.display_bucket != "valid":
                print("")
                print(format_pullback_intelligence_block(symbol_result))
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


def _format_available_command_presets() -> str:
    lines = ["Available command presets:"]
    for name, preset in COMMAND_PRESETS.items():
        lines.append(
            f"- {name}: modes {','.join(preset.modes)}; "
            f"timeframes {preset.htf_timeframe}>{preset.bias_timeframe}>"
            f"{preset.structure_timeframe}>"
            f"{preset.execution_timeframe}>{preset.confirmation_timeframe}; "
            f"min score {preset.min_score_for_idea}; min RR {_display(preset.min_rr)}"
        )
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


def _performance_memory_enabled(args: argparse.Namespace) -> bool:
    if args.performance_memory is not None:
        return bool(args.performance_memory)
    return bool(args.replay or args.command_preset == "daily")


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
    try:
        settings = Settings()
    except Exception:
        settings = None
    if settings is not None and bool(settings.telegram_commands_enabled):
        warnings.append("Telegram commands enabled in config, but command listener must be run separately.")
    if (
        settings is not None
        and _telegram_lifecycle_public_delivery_enabled(args)
        and settings.telegram_signals_enabled
    ):
        destination = resolve_public_signal_destination(settings)
        if destination.warning != NA:
            warnings.append(destination.warning)
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
    lines = [
        f"Universe mode: {universe.mode}",
        f"Universe label: {universe.label}",
        f"Universe source: {universe.source}",
        f"Universe size requested: {universe.requested_size}",
        f"Symbols resolved: {len(universe.resolved_symbols)}",
        f"Excluded count: {len(universe.excluded_symbols)}",
        f"{top_label}: {top_text}",
    ]
    diagnostics = universe.diagnostics
    if diagnostics:
        final_ranks = [
            universe.market_cap_rank_by_symbol[symbol]
            for symbol in universe.resolved_symbols
            if symbol in universe.market_cap_rank_by_symbol
        ]
        lines.extend(
            (
                f"Provider assets: {diagnostics.get('provider_asset_count', 0)}",
                f"Valid ranks: {diagnostics.get('valid_rank_count', 0)}",
                f"Ranks within boundary: {diagnostics.get('rank_within_boundary_count', 0)}",
                f"Binance crypto perpetual matches: {diagnostics.get('binance_perp_match_count', 0)}",
                f"Ranks greater than N excluded: {diagnostics.get('rank_gt_n_excluded_count', 0)}",
                f"Ambiguous tickers excluded: {diagnostics.get('ambiguous_symbol_count', 0)}",
                f"Maximum final global rank: {max(final_ranks) if final_ranks else 'N/A'}",
                f"Universe cache used: {'yes' if diagnostics.get('cache_used') else 'no'}",
            )
        )
    return "\n".join(lines)


async def _resolve_watchlist_for_scan(args: argparse.Namespace) -> WatchlistResolution:
    if args.universe == MANUAL_UNIVERSE_MODE:
        return _resolve_watchlist(args)
    return await _resolve_universe_watchlist(args)


async def _resolve_watchlist_for_args(args: argparse.Namespace) -> WatchlistResolution:
    if args.watch and args.watch_symbols_from_latest_run:
        membership_boundary = None
        if args.universe == BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE:
            membership_boundary = await _resolve_watchlist_for_scan(args)
        return _resolve_watchlist_from_latest_run(args, membership_boundary=membership_boundary)

    watchlist = await _resolve_watchlist_for_scan(args)
    if args.watch and args.watch_only_near_misses:
        return _filter_watchlist_to_prior_watch_symbols(args, watchlist)
    if args.continue_watch and not args.watch:
        return _extend_watchlist_for_continue_watch(args, watchlist)
    return watchlist


def _resolve_watchlist_from_latest_run(
    args: argparse.Namespace,
    *,
    membership_boundary: WatchlistResolution | None = None,
) -> WatchlistResolution:
    try:
        symbols = load_symbols_from_run(LATEST_RUN_PATH, near_miss_only=args.watch_only_near_misses)
    except WatchModeError as exc:
        raise SystemExit(str(exc)) from exc
    if not symbols:
        label = "near-miss symbols" if args.watch_only_near_misses else "symbols"
        raise SystemExit(f"No {label} found in latest saved run file: {LATEST_RUN_PATH}")
    if membership_boundary is None:
        return _watch_resolution_from_symbols(args, symbols, source_label=f"latest run {LATEST_RUN_PATH}")

    allowed = set(membership_boundary.symbols)
    filtered = tuple(symbol for symbol in dedupe_symbols(symbols) if symbol in allowed)
    ignored = tuple(symbol for symbol in dedupe_symbols(symbols) if symbol not in allowed)
    if not filtered:
        raise SystemExit(
            "Latest saved-run symbols do not intersect the current strict market-cap universe."
        )
    ignored_membership = dedupe_symbols(
        (*membership_boundary.membership_boundary_ignored_symbols, *ignored)
    )
    universe = membership_boundary.universe.with_resolved_symbols(
        filtered,
        diagnostic_updates={"membership_boundary_ignored_count": len(ignored_membership)},
    )
    return WatchlistResolution(
        symbols=filtered,
        source_label=f"latest run {LATEST_RUN_PATH} within strict market-cap membership",
        universe=universe,
        explicit_excluded_symbols=membership_boundary.explicit_excluded_symbols,
        pre_cap_symbols_count=membership_boundary.pre_cap_symbols_count,
        queue_cap_applied=membership_boundary.queue_cap_applied,
        membership_boundary_ignored_symbols=ignored_membership,
    )


def _filter_watchlist_to_prior_watch_symbols(
    args: argparse.Namespace,
    watchlist: WatchlistResolution,
) -> WatchlistResolution:
    try:
        state = load_watch_state(WATCH_STATE_PATH)
    except WatchModeError as exc:
        raise SystemExit(str(exc)) from exc

    symbols = state_watch_symbols(state, watchlist.symbols)
    if not symbols and LATEST_RUN_PATH.exists():
        try:
            latest_symbols = load_symbols_from_run(LATEST_RUN_PATH, near_miss_only=True)
        except WatchModeError as exc:
            raise SystemExit(str(exc)) from exc
        watchlist_set = set(watchlist.symbols)
        symbols = tuple(symbol for symbol in latest_symbols if symbol in watchlist_set)

    if not symbols:
        raise SystemExit(
            "--watch-only-near-misses did not find prior NEAR MISS, HOT WATCH, or WATCH symbols. "
            "Use --watch-symbols-from-latest-run or run a scan with --save-run first."
        )

    if watchlist.universe.strict_membership:
        universe = watchlist.universe.with_resolved_symbols(symbols)
        return WatchlistResolution(
            symbols=tuple(symbols),
            source_label=f"{watchlist.source_label} prior near-miss/watch symbols",
            universe=universe,
            explicit_excluded_symbols=watchlist.explicit_excluded_symbols,
            pre_cap_symbols_count=watchlist.pre_cap_symbols_count,
            queue_cap_applied=watchlist.queue_cap_applied,
            membership_boundary_ignored_symbols=watchlist.membership_boundary_ignored_symbols,
        )
    return _watch_resolution_from_symbols(
        args,
        symbols,
        source_label=f"{watchlist.source_label} prior near-miss/watch symbols",
    )


def _extend_watchlist_for_continue_watch(
    args: argparse.Namespace,
    watchlist: WatchlistResolution,
) -> WatchlistResolution:
    try:
        state = load_watch_state(WATCH_STATE_PATH)
    except WatchModeError as exc:
        raise SystemExit(str(exc)) from exc

    state_symbols = state_watch_symbols(state, tuple(state.symbols))
    latest_symbols: tuple[str, ...] = ()
    if LATEST_RUN_PATH.exists():
        try:
            latest_symbols = load_symbols_from_run(LATEST_RUN_PATH, near_miss_only=True)
        except WatchModeError as exc:
            raise SystemExit(str(exc)) from exc

    continued_symbols = dedupe_symbols((*state_symbols, *latest_symbols))
    if watchlist.universe.strict_membership:
        allowed = set(watchlist.symbols)
        allowed_continued = tuple(symbol for symbol in continued_symbols if symbol in allowed)
        ignored = tuple(symbol for symbol in continued_symbols if symbol not in allowed)
        args.continued_watch_symbols = allowed_continued
        if not ignored:
            return watchlist
        ignored_membership = dedupe_symbols((*watchlist.membership_boundary_ignored_symbols, *ignored))
        return replace(
            watchlist,
            membership_boundary_ignored_symbols=ignored_membership,
            universe=watchlist.universe.with_resolved_symbols(
                watchlist.symbols,
                diagnostic_updates={"membership_boundary_ignored_count": len(ignored_membership)},
            ),
        )

    args.continued_watch_symbols = continued_symbols
    if not continued_symbols:
        return watchlist

    combined = dedupe_symbols((*watchlist.symbols, *continued_symbols))
    pre_cap_count = len(combined)
    if args.max_symbols is not None:
        if args.max_symbols < 1:
            raise SystemExit("--max-symbols must be at least 1.")
        combined = combined[: args.max_symbols]
    cap_applied = len(combined) < pre_cap_count
    if combined == watchlist.symbols and not cap_applied:
        return watchlist
    if combined == watchlist.symbols:
        return WatchlistResolution(
            symbols=watchlist.symbols,
            source_label=watchlist.source_label,
            universe=watchlist.universe,
            explicit_excluded_symbols=watchlist.explicit_excluded_symbols,
            pre_cap_symbols_count=pre_cap_count,
            queue_cap_applied=True,
        )
    return WatchlistResolution(
        symbols=combined,
        source_label=f"{watchlist.source_label} + continued watch candidates",
        universe=manual_symbol_universe(combined, requested_size=len(combined)),
        explicit_excluded_symbols=watchlist.explicit_excluded_symbols,
        pre_cap_symbols_count=pre_cap_count,
        queue_cap_applied=cap_applied,
    )


def _watch_resolution_from_symbols(
    args: argparse.Namespace,
    symbols: Sequence[str],
    *,
    source_label: str,
) -> WatchlistResolution:
    try:
        normalized_symbols = validate_symbols(symbols, context=source_label)
        exclude_symbols = set(validate_symbols(args.exclude_symbols, context="--exclude-symbols"))
    except WatchlistPresetError as exc:
        raise SystemExit(str(exc)) from exc

    excluded_cli_symbols = tuple(symbol for symbol in normalized_symbols if symbol in exclude_symbols)
    pre_cap_symbols = tuple(symbol for symbol in dedupe_symbols(normalized_symbols) if symbol not in exclude_symbols)
    resolved_symbols = pre_cap_symbols
    if args.max_symbols is not None:
        if args.max_symbols < 1:
            raise SystemExit("--max-symbols must be at least 1.")
        resolved_symbols = resolved_symbols[: args.max_symbols]
    if not resolved_symbols:
        raise SystemExit("Resolved watch mode symbol list is empty.")

    universe = manual_symbol_universe(
        resolved_symbols,
        requested_size=len(resolved_symbols),
        excluded_symbols=excluded_cli_symbols,
        min_quote_volume=args.min_quote_volume,
    )
    return WatchlistResolution(
        symbols=resolved_symbols,
        source_label=source_label,
        universe=universe,
        explicit_excluded_symbols=excluded_cli_symbols,
        pre_cap_symbols_count=len(pre_cap_symbols),
        queue_cap_applied=len(resolved_symbols) < len(pre_cap_symbols),
    )


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
    pre_cap_symbols = resolved_symbols

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
    return WatchlistResolution(
        symbols=resolved_symbols,
        source_label=source_label,
        universe=universe,
        explicit_excluded_symbols=excluded_cli_symbols,
        pre_cap_symbols_count=len(pre_cap_symbols),
        queue_cap_applied=len(resolved_symbols) < len(pre_cap_symbols),
    )


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

    membership_boundary_ignored_symbols: tuple[str, ...] = ()
    if universe.strict_membership:
        allowed = set(universe.resolved_symbols)
        membership_boundary_ignored_symbols = tuple(
            symbol for symbol in include_symbols if symbol not in allowed
        )
        pre_exclude_symbols = tuple(universe.resolved_symbols)
    else:
        pre_exclude_symbols = dedupe_symbols((*universe.resolved_symbols, *include_symbols))
    excluded_cli_symbols = tuple(symbol for symbol in pre_exclude_symbols if symbol in exclude_symbols)
    resolved_symbols = pre_exclude_symbols
    if exclude_symbols:
        resolved_symbols = tuple(symbol for symbol in resolved_symbols if symbol not in exclude_symbols)
    pre_cap_symbols = resolved_symbols

    if args.max_symbols is not None:
        if args.max_symbols < 1:
            raise SystemExit("--max-symbols must be at least 1.")
        resolved_symbols = resolved_symbols[: args.max_symbols]

    if not resolved_symbols:
        raise SystemExit(
            "Resolved watchlist is empty after universe/include/exclude/max-symbols processing. Provide at least one symbol."
        )

    universe = universe.with_resolved_symbols(
        resolved_symbols,
        extra_excluded_symbols=excluded_cli_symbols,
        diagnostic_updates={
            "requested_max_symbols": args.max_symbols,
            "membership_boundary_ignored_count": len(membership_boundary_ignored_symbols),
        }
        if universe.strict_membership
        else None,
    )
    return WatchlistResolution(
        symbols=resolved_symbols,
        source_label=f"universe {args.universe}",
        universe=universe,
        explicit_excluded_symbols=excluded_cli_symbols,
        pre_cap_symbols_count=len(pre_cap_symbols),
        queue_cap_applied=len(resolved_symbols) < len(pre_cap_symbols),
        membership_boundary_ignored_symbols=membership_boundary_ignored_symbols,
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


def _resume_state_with_active_lifecycle_retry(
    resume_state: ResumeState,
    active_symbols: Sequence[str],
) -> ResumeState:
    active_set = set(active_symbols)
    if not active_set:
        return resume_state
    skipped_symbols = tuple(symbol for symbol in resume_state.skipped_symbols if symbol not in active_set)
    if skipped_symbols == resume_state.skipped_symbols:
        return resume_state
    return ResumeState(
        results_by_symbol=resume_state.results_by_symbol,
        skipped_symbols=skipped_symbols,
        loaded_symbols=resume_state.loaded_symbols,
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
    *,
    symbol_queue_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        "resume_from": str(args.resume_from) if args.resume_from is not None else None,
        "save_run": str(args.save_run) if args.save_run is not None else None,
        "resume_skip_enabled": not args.no_resume_skip,
        "loaded_symbols": list(resume_state.loaded_symbols),
        "skipped_symbols": list(resume_state.skipped_symbols),
        "symbols_to_scan": list(symbols_to_scan),
        "watchlist_symbols": list(watchlist_symbols),
        "universe": universe.to_json(),
    }
    if symbol_queue_diagnostics is not None:
        metadata["symbol_queue"] = dict(symbol_queue_diagnostics)
    return metadata


def _combined_run_result(
    *,
    config: ScannerRunConfig,
    watchlist_symbols: Sequence[str],
    results_by_symbol: dict[str, ScannerSymbolResult],
    cache: MarketDataCache | None,
    retry_diagnostics: Sequence[dict[str, Any]],
    resume_metadata: Mapping[str, Any],
    runtime_stats: ScannerRuntimeStats | None,
    market_regime: Any | None = None,
    global_context: Any | None = None,
) -> ScannerRunResult:
    ordered_results = tuple(
        results_by_symbol[symbol]
        for symbol in watchlist_symbols
        if symbol in results_by_symbol
    )
    cache_stats = cache.stats() if cache is not None else _empty_cache_stats(config)
    effective_market_regime = market_regime
    if effective_market_regime is None:
        effective_market_regime = (
            disabled_market_regime_result()
            if not config.market_regime_enabled
            else default_market_regime_result()
        )
    queue_total = len(watchlist_symbols)
    queue_metadata = resume_metadata.get("symbol_queue")
    if isinstance(queue_metadata, Mapping):
        queue_total = int(queue_metadata.get("final_queued_count", queue_total))
    return ScannerRunResult(
        config=config,
        results=ordered_results,
        scanned_symbols=sum(
            1 for result in ordered_results if result.iteration_outcome != "not_run"
        ),
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
            total_symbols=queue_total,
            runtime_stats=runtime_stats,
        ),
        market_regime=effective_market_regime,
        regime_adjustments=effective_market_regime.adjustment,
        regime_warnings=effective_market_regime.warnings,
        global_context=global_context,
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

    outcomes = Counter(result.iteration_outcome or "errored" for result in results)
    evaluated_symbols = outcomes["evaluated"]
    rejected_symbols = outcomes["rejected"]
    timed_out_symbols = outcomes["timed_out"]
    not_run_symbols = outcomes["not_run"]
    errored_symbols = outcomes["errored"] + timed_out_symbols
    skipped_symbols = not_run_symbols
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
        timeout_count=timed_out_symbols,
        completed_symbols=evaluated_symbols + rejected_symbols,
        skipped_symbols=skipped_symbols,
        errored_symbols=errored_symbols,
        skipped_errored_symbols=skipped_symbols + errored_symbols,
        global_timeout_hit=global_timeout_hit,
        queued_symbols=total_symbols,
        evaluated_symbols=evaluated_symbols,
        rejected_symbols=rejected_symbols,
        timed_out_symbols=timed_out_symbols,
        not_run_symbols=not_run_symbols,
        outcome_counts={
            "evaluated": evaluated_symbols,
            "rejected": rejected_symbols,
            "errored": outcomes["errored"],
            "timed_out": timed_out_symbols,
            "not_run": not_run_symbols,
        },
        process_memory=(
            runtime_stats.process_memory
            if runtime_stats is not None
            else ScannerProcessMemoryStats()
        ),
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
    portfolio_selection: PortfolioSelectionResult | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            _json_payload(
                result,
                ranked_results,
                replay_summary=replay_summary,
                portfolio_selection=portfolio_selection,
            ),
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_export_json(
    path: Path,
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[Any] | None = None,
    replay_summary: ReplaySummary | None = None,
    portfolio_selection: PortfolioSelectionResult | None = None,
    command_center: Mapping[str, Any] | None = None,
) -> None:
    payload = _json_payload(
        result,
        ranked_results,
        replay_summary=replay_summary,
        portfolio_selection=portfolio_selection,
    )
    if command_center is not None:
        payload["command_center"] = dict(command_center)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _append_scan_run_manifest(
    result: ScannerRunResult,
    *,
    watchlist: WatchlistResolution,
    ranked_results: Sequence[Any] | None,
    manifest_path: Path,
    nightly_history_path: Path,
    run_id: str | None = None,
    watch_iteration: int | None = None,
    output_scan_path: Path | None = None,
    latest_scan_path: Path | None = None,
    watch_summary: WatchIterationSummary | None = None,
) -> dict[str, Any]:
    row = _scan_run_manifest_row(
        result,
        watchlist=watchlist,
        ranked_results=ranked_results,
        run_id=run_id,
        watch_iteration=watch_iteration,
        output_scan_path=output_scan_path,
        latest_scan_path=latest_scan_path,
    )
    if watch_summary is not None:
        row["watch_supervisor"] = watch_summary.model_dump(mode="json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
        handle.write("\n")
    _write_nightly_scan_history(nightly_history_path, row)
    return row


def _scan_run_manifest_row(
    result: ScannerRunResult,
    *,
    watchlist: WatchlistResolution,
    ranked_results: Sequence[Any] | None,
    run_id: str | None = None,
    watch_iteration: int | None = None,
    output_scan_path: Path | None = None,
    latest_scan_path: Path | None = None,
) -> dict[str, Any]:
    ranked = tuple(ranked_results) if ranked_results is not None else rank_scan_results(result.results)
    bucket_counts = Counter(item.display.display_bucket for item in ranked)
    failed_stage_counts = Counter(
        item.display.failed_stage for item in ranked if _display(item.display.failed_stage) != NA
    )
    lifecycle_state_counts = Counter(
        result.lifecycle_state.current_state.value
        for result in (item.symbol_result for item in ranked)
        if result.lifecycle_state is not None
    )
    actionability_counts = _manifest_actionability_counts(ranked)
    actionable_a_grade_count = actionability_counts["actionable_a_grade_setups"]
    confirmed_setup_count = lifecycle_state_counts.get("CONFIRMED", 0)
    target_integrity_blocks = sum(
        1
        for item in ranked
        if item.display.failed_stage == "target_integrity" and item.symbol_result.alert_result is None
    )
    runtime = result.runtime_stats
    row: dict[str, Any] = {
        "run_id": _manifest_run_id(result, run_id),
        "timestamp": _watch_iteration_timestamp(),
        "universe_mode": _display(getattr(watchlist.universe, "mode", NA)),
        "universe_label": _display(getattr(watchlist.universe, "label", watchlist.source_label)),
        "symbols_scanned": result.scanned_symbols,
        "valid_setup_count": bucket_counts.get("valid", 0),
        "actionable_setup_count": actionable_a_grade_count,
        "candidate_a_grade_setups": actionability_counts["candidate_a_grade_setups"],
        "actionable_a_grade_setups": actionable_a_grade_count,
        "actionable_a_grade_count": actionable_a_grade_count,
        "actionable_a_grade_target_caution": actionability_counts["actionable_a_grade_target_caution"],
        "blocked_a_grade_by_scoring": actionability_counts["blocked_a_grade_by_scoring"],
        "blocked_a_grade_by_target": actionability_counts["blocked_a_grade_by_target"],
        "blocked_a_grade_by_entry_window": actionability_counts["blocked_a_grade_by_entry_window"],
        "blocked_a_grade_by_trust": actionability_counts["blocked_a_grade_by_trust"],
        "fatal_target_blocks": actionability_counts["fatal_target_blocks"],
        "soft_target_warnings": actionability_counts["soft_target_warnings"],
        "confirmed_setups": confirmed_setup_count,
        "confirmed_setup_count": confirmed_setup_count,
        "near_miss_count": bucket_counts.get("near_miss", 0),
        "rejected_count": bucket_counts.get("no_setup", 0),
        "failed_symbol_count": result.failed_symbols,
        "minimum_rr_policy": build_minimum_rr_policy_payload(result),
        "minimum_rr_audit": build_minimum_rr_audit(result),
        "timeout_count": runtime.timeout_count,
        "failed_stage_counts": dict(sorted(failed_stage_counts.items())),
        "lifecycle_state_counts": dict(sorted(lifecycle_state_counts.items())),
        "alerts_created": result.dry_run_alerts_created,
        "alerts_blocked_by_target_integrity": target_integrity_blocks,
        "journal_entries_created": result.journal_entries_created,
        "trade_ideas_created": result.trade_ideas_created,
        "market_regime": _display(getattr(result.market_regime.state, "value", result.market_regime.state)),
        "regime_confidence": _json_scalar(result.market_regime.confidence_score),
        "runtime_seconds": runtime.total_runtime_seconds,
        "average_seconds_per_symbol": runtime.average_seconds_per_symbol,
        "process_memory": runtime.process_memory.model_dump(mode="json"),
    }
    if watch_iteration is not None:
        row["watch_iteration"] = watch_iteration
    queue_diagnostics = result.resume_metadata.get("symbol_queue") if isinstance(result.resume_metadata, Mapping) else None
    if isinstance(queue_diagnostics, Mapping):
        row["symbol_queue"] = dict(queue_diagnostics)
    if output_scan_path is not None:
        row["output_scan_path"] = str(output_scan_path)
    if latest_scan_path is not None:
        row["latest_scan_path"] = str(latest_scan_path)
    return row


def _manifest_actionability_counts(ranked_results: Sequence[Any]) -> dict[str, int]:
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
    for item in ranked_results:
        symbol_result = item.symbol_result
        lifecycle = getattr(symbol_result, "lifecycle_state", None)
        diagnostics = representative_strategy_diagnostics(symbol_result)
        actionability_state = _display(
            _first_manifest_non_na(
                getattr(symbol_result, "actionability_state", NA),
                getattr(lifecycle, "actionability_state", NA),
                diagnostics.get("actionability_state"),
            )
        )
        quality = getattr(symbol_result, "setup_quality", None)
        raw_grade = getattr(quality, "quality_grade", NA)
        candidate_grade = _display(
            _first_manifest_non_na(
                getattr(symbol_result, "candidate_quality_grade", NA),
                getattr(lifecycle, "candidate_quality_grade", NA),
                getattr(raw_grade, "value", raw_grade),
                diagnostics.get("candidate_quality_grade"),
                diagnostics.get("quality_grade"),
            )
        )
        lifecycle_state = _display(getattr(getattr(lifecycle, "current_state", NA), "value", getattr(lifecycle, "current_state", NA)))
        severity_key = _manifest_status_key(
            _first_manifest_non_na(
                getattr(symbol_result, "target_failure_severity", NA),
                getattr(lifecycle, "target_failure_severity", NA),
                diagnostics.get("target_failure_severity"),
            )
        )
        state_key = _manifest_status_key(actionability_state)
        is_a_candidate = candidate_grade.strip().upper() in {"A-", "A", "A+"} or actionability_state.startswith("A_GRADE_")
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


def _manifest_status_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _first_manifest_non_na(*values: Any) -> Any:
    for value in values:
        if _display(value) != NA:
            return value
    return NA


async def _route_admin_report(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[Any],
    manifest_row: Mapping[str, Any],
    console_presenter: ScannerConsolePresenter | None = None,
) -> None:
    try:
        route_result = await route_admin_scan_report(
            result,
            ranked_results=ranked_results,
            manifest_row=manifest_row,
            settings=Settings(),
            drafts_dir=ADMIN_DRAFTS_DIR,
        )
    except Exception as exc:
        if console_presenter is not None:
            if not console_presenter.compact:
                console_presenter.emit(f"Telegram admin reporting warning: {type(exc).__name__}")
            return
        print(f"Warning: Telegram admin reporting failed safely: {type(exc).__name__}")
        return

    path_text = route_result.draft_path.as_posix() if route_result.draft_path is not None else "N/A"
    if console_presenter is not None:
        if not console_presenter.compact:
            console_presenter.emit(
                "Telegram admin drafts: "
                f"{route_result.delivery_status}; {route_result.drafts_created} new, "
                f"{route_result.drafts_skipped_duplicate} duplicate; path {path_text}"
            )
            if route_result.warning != NA:
                console_presenter.emit(f"Telegram admin warning: {route_result.warning}")
        return
    print(
        "Telegram admin drafts: "
        f"{route_result.delivery_status}; "
        f"{route_result.drafts_created} new, {route_result.drafts_skipped_duplicate} duplicate; "
        f"path {path_text}"
    )
    if route_result.warning != NA:
        print(f"Warning: {route_result.warning}")


def _manifest_run_id(result: ScannerRunResult, explicit_run_id: str | None) -> str:
    if explicit_run_id:
        return explicit_run_id
    existing = _result_run_id(result)
    if existing != NA:
        return existing
    return uuid4().hex


def _result_run_id(result: ScannerRunResult) -> str:
    metadata = result.resume_metadata if isinstance(result.resume_metadata, Mapping) else {}
    for key in ("scan_run_id", "run_id", "storage_run_id"):
        value = _display(metadata.get(key))
        if value != NA:
            return value
    return NA


def _json_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _display(value)
    if hasattr(value, "value"):
        return getattr(value, "value")
    if value is None:
        return NA
    return value


def _write_nightly_scan_history(path: Path, row: Mapping[str, Any], *, max_runs: int = 200) -> None:
    previous_runs: list[Mapping[str, Any]] = []
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, Mapping) and isinstance(payload.get("runs"), list):
            previous_runs = [item for item in payload["runs"] if isinstance(item, Mapping)]
        elif isinstance(payload, list):
            previous_runs = [item for item in payload if isinstance(item, Mapping)]
    runs = [*previous_runs, dict(row)][-max_runs:]
    payload = {
        "schema_version": "nightly_scan_history_v1",
        "updated_at": row.get("timestamp", _watch_iteration_timestamp()),
        "runs": runs,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def _write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _write_backtest_json(path: Path, replay_summary: ReplaySummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(backtest_json_payload(replay_summary), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_edge_json(path: Path, edge_report: EdgeAnalyticsReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "edge_analytics": edge_report.model_dump(mode="json"),
                "expectancy_metrics": edge_report.expectancy_metrics.model_dump(mode="json"),
                "confidence_label": edge_report.confidence_label,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _handle_history_command(args: argparse.Namespace) -> None:
    try:
        history = list_scan_history(args.database_path, limit=args.history_limit)
        if args.export_history_json is not None:
            _write_history_json(
                args.export_history_json,
                export_history_payload(args.database_path, limit=args.history_limit),
            )
            print(f"Exported scan history: {args.export_history_json}")
        if args.show_history:
            print(format_history_table(history))
    except StorageError as exc:
        raise SystemExit(str(exc)) from exc


def _handle_research_command(args: argparse.Namespace) -> None:
    filters = ResearchFilters(
        symbol=args.research_symbol,
        mode=args.research_mode,
        regime=args.research_regime,
        limit=args.research_limit,
        lifecycle_stale_hours=args.lifecycle_stale_hours,
    )
    try:
        report = build_research_report(
            args.database_path,
            query=args.research_query,
            filters=filters,
        )
    except ResearchDatabaseMissing as exc:
        print(str(exc))
        return
    except StorageError as exc:
        raise SystemExit(str(exc)) from exc

    if args.research_output_json is not None:
        _write_research_json(args.research_output_json, report)
        print(f"Exported research report: {args.research_output_json}")
        return
    print(format_research_report(report))


def _lifecycle_enabled(args: argparse.Namespace) -> bool:
    if args.lifecycle is not None:
        return bool(args.lifecycle)
    return bool(
        args.watch
        or args.store_scan
        or args.show_lifecycle
        or _telegram_lifecycle_public_delivery_enabled(args)
    )


def _lifecycle_scan_run_id_enabled(args: argparse.Namespace) -> bool:
    return bool(
        _lifecycle_enabled(args)
        and (args.store_scan or _telegram_lifecycle_public_delivery_enabled(args))
    )


def _reset_lifecycle_state(args: argparse.Namespace) -> None:
    try:
        SetupLifecycleService(args.database_path).reset()
    except StorageError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Reset lifecycle state: {args.database_path}")


def _apply_lifecycle_if_enabled(
    args: argparse.Namespace,
    result: ScannerRunResult,
    *,
    scan_run_id: str | None = None,
) -> ScannerRunResult:
    if not _lifecycle_enabled(args):
        return result
    try:
        return apply_lifecycle_to_run_result(result, database_path=args.database_path, scan_run_id=scan_run_id)
    except StorageError as exc:
        raise SystemExit(str(exc)) from exc


def _telegram_manual_signal_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


def _telegram_manual_signals_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "telegram_manual_signals", False))


def _telegram_lifecycle_public_delivery_enabled(args: argparse.Namespace) -> bool:
    """Return whether this command selected the canonical lifecycle-backed public route."""

    if _telegram_manual_signals_enabled(args):
        return True
    return bool(getattr(args, "watch", False) and getattr(args, "telegram_live_alerts", False))


def _legacy_watch_activation_delivery_enabled(args: argparse.Namespace) -> bool:
    """Keep the legacy activation path local-only when no public route was selected."""

    return not _telegram_lifecycle_public_delivery_enabled(args)


def _telegram_manual_lifecycle_status_label(args: argparse.Namespace) -> str:
    if not _telegram_lifecycle_public_delivery_enabled(args):
        return "disabled"
    try:
        settings = Settings()
    except Exception:
        return "blocked by settings"
    if not settings.local_manual_mode:
        return "disabled (LOCAL_MANUAL_MODE=false)"
    if settings.telegram_signals_enabled:
        return "enabled"
    return "enabled (sending disabled)"


def _telegram_admin_draft_status_label() -> str:
    try:
        config = TelegramAdminConfig.from_settings(Settings())
    except Exception:
        return "blocked by settings"
    if not config.admin_report_enabled:
        return "disabled/dry-run" if config.dry_run else "disabled"
    return "dry-run" if config.dry_run else "enabled"


async def _deliver_telegram_manual_signals_if_enabled(
    args: argparse.Namespace,
    result: ScannerRunResult,
    *,
    scan_run_id: str | None,
    print_summary: bool = True,
) -> TelegramLifecycleDeliverySummary | None:
    if not _telegram_lifecycle_public_delivery_enabled(args):
        return None
    settings = _telegram_manual_signal_settings()
    try:
        summary = await TelegramLifecycleDeliveryService(
            database_path=args.database_path,
            settings=settings,
            min_rr=result.config.min_rr,
            min_score_for_idea=Decimal(args.min_score_for_idea),
        ).deliver_for_run(result, scan_run_id=scan_run_id)
    except StorageError as exc:
        raise SystemExit(str(exc)) from exc
    if print_summary:
        _print_telegram_manual_lifecycle_summary(summary)
    return summary


def _print_telegram_manual_lifecycle_summary(summary: TelegramLifecycleDeliverySummary) -> None:
    print("Telegram manual lifecycle summary:")
    print(f"- sent: {summary.sent}")
    print(f"- duplicates skipped: {summary.duplicate}")
    print(f"- blocked: {summary.blocked}")
    print(f"- blocked repeats compacted: {summary.blocked_repeat}")
    print(f"- failed: {summary.failed}")


def _watchlist_with_lifecycle_priority(
    args: argparse.Namespace,
    watchlist: WatchlistResolution,
) -> WatchlistResolution:
    if not _lifecycle_enabled(args):
        return watchlist
    original_symbols = watchlist.symbols
    try:
        all_active_symbols = active_lifecycle_symbols(original_symbols, database_path=args.database_path)
    except StorageError as exc:
        raise SystemExit(str(exc)) from exc

    lifecycle_membership_ignored_symbols: tuple[str, ...] = ()
    active_symbols = all_active_symbols
    if watchlist.universe.strict_membership:
        allowed = set(watchlist.universe.resolved_symbols)
        active_symbols = tuple(symbol for symbol in all_active_symbols if symbol in allowed)
        lifecycle_membership_ignored_symbols = tuple(
            symbol for symbol in all_active_symbols if symbol not in allowed
        )

    active_set = set(active_symbols)
    discovery_symbols = tuple(symbol for symbol in original_symbols if symbol not in active_set)
    uncapped_symbols = dedupe_symbols((*active_symbols, *discovery_symbols))
    if args.max_symbols is not None:
        if args.max_symbols < 1:
            raise SystemExit("--max-symbols must be at least 1.")
        discovery_capacity = max(0, args.max_symbols - len(active_symbols))
        discovery_symbols = discovery_symbols[:discovery_capacity]
    prioritized = dedupe_symbols((*active_symbols, *discovery_symbols))

    original_set = set(original_symbols)
    prioritized_set = set(prioritized)
    dropped_symbols = tuple(symbol for symbol in original_symbols if symbol not in prioritized_set)
    added_symbols = tuple(symbol for symbol in prioritized if symbol not in original_set)
    original_index = {symbol: index for index, symbol in enumerate(original_symbols)}
    promoted_symbols = tuple(
        symbol
        for index, symbol in enumerate(prioritized)
        if symbol in original_index and index < original_index[symbol]
    )
    active_over_cap_count = (
        max(0, len(active_symbols) - args.max_symbols)
        if args.max_symbols is not None
        else 0
    )
    cap_applied = watchlist.queue_cap_applied or len(prioritized) < len(uncapped_symbols)
    pre_cap_count = max(
        watchlist.pre_cap_symbols_count or len(original_symbols),
        len(uncapped_symbols),
    )
    source_label = (
        watchlist.source_label
        if prioritized == original_symbols or watchlist.source_label.endswith(" + lifecycle priority")
        else f"{watchlist.source_label} + lifecycle priority"
    )
    ignored_membership = dedupe_symbols(
        (*watchlist.membership_boundary_ignored_symbols, *lifecycle_membership_ignored_symbols)
    )
    ignored_lifecycle = dedupe_symbols(
        (*watchlist.lifecycle_membership_ignored_symbols, *lifecycle_membership_ignored_symbols)
    )
    if (
        prioritized == original_symbols
        and watchlist.active_lifecycle_symbols == active_symbols
        and watchlist.lifecycle_membership_ignored_symbols == ignored_lifecycle
    ):
        return watchlist
    universe = watchlist.universe
    if universe.strict_membership:
        universe = universe.with_resolved_symbols(
            universe.resolved_symbols,
            diagnostic_updates={"membership_boundary_ignored_count": len(ignored_membership)},
        )
    return WatchlistResolution(
        symbols=prioritized,
        source_label=source_label,
        universe=universe,
        explicit_excluded_symbols=watchlist.explicit_excluded_symbols,
        pre_cap_symbols_count=pre_cap_count,
        queue_cap_applied=cap_applied,
        lifecycle_priority_promoted_symbols=promoted_symbols,
        lifecycle_priority_added_symbols=added_symbols,
        lifecycle_priority_dropped_symbols=dropped_symbols,
        active_lifecycle_symbols=active_symbols,
        active_lifecycle_over_cap_count=active_over_cap_count,
        lifecycle_capacity_displaced_symbols=dropped_symbols,
        membership_boundary_ignored_symbols=ignored_membership,
        lifecycle_membership_ignored_symbols=ignored_lifecycle,
    )


def _adaptive_symbol_priority_enabled(args: argparse.Namespace, watchlist: WatchlistResolution) -> bool:
    if args.adaptive_symbol_priority is not None:
        return bool(args.adaptive_symbol_priority)
    return bool(args.watch or args.universe_size >= 100 or len(watchlist.symbols) >= 100)


def _symbol_priority_plan_for_watchlist(
    args: argparse.Namespace,
    watchlist: WatchlistResolution,
) -> SymbolPriorityPlan:
    enabled = _adaptive_symbol_priority_enabled(args, watchlist)
    if not enabled:
        return empty_symbol_priority_plan(watchlist.symbols, enabled=False)
    try:
        health_records = load_symbol_health_records(args.database_path, watchlist.symbols)
        lifecycle_states = _lifecycle_states_for_symbols(args, watchlist.symbols)
    except StorageError as exc:
        raise SystemExit(str(exc)) from exc
    return build_symbol_priority_plan(
        watchlist.symbols,
        health_records,
        lifecycle_states=lifecycle_states,
        enabled=True,
    )


def _lifecycle_states_for_symbols(args: argparse.Namespace, symbols: Sequence[str]) -> dict[str, str]:
    if not symbols:
        return {}
    try:
        with SQLiteSetupLifecycleRepository(args.database_path) as repository:
            records = repository.get_records_for_symbols(symbols)
    except StorageError:
        raise
    output: dict[str, str] = {}
    best_rank: dict[str, int] = {}
    for record in records:
        state = record.current_state.value
        rank = lifecycle_monitoring_priority(record.current_state)
        if record.symbol not in output or rank < best_rank[record.symbol]:
            output[record.symbol] = state
            best_rank[record.symbol] = rank
    return output


def _queued_symbols_for_scan(
    args: argparse.Namespace,
    watchlist: WatchlistResolution,
    symbol_priority_plan: SymbolPriorityPlan,
) -> tuple[str, ...]:
    del args
    queued = symbol_priority_plan.symbols_to_scan if symbol_priority_plan.enabled else watchlist.symbols
    if not watchlist.universe.strict_membership:
        return queued
    allowed = set(watchlist.universe.resolved_symbols)
    return tuple(symbol for symbol in queued if symbol in allowed)


def _symbol_queue_diagnostics(
    args: argparse.Namespace,
    watchlist: WatchlistResolution,
    symbol_priority_plan: SymbolPriorityPlan,
    queued_symbols: Sequence[str],
) -> dict[str, Any]:
    requested_symbols = watchlist.symbols
    queued = tuple(queued_symbols)
    queued_set = set(queued)
    health_excluded = tuple(symbol_priority_plan.skipped_symbols) if symbol_priority_plan.enabled else ()
    health_excluded_set = set(health_excluded)
    cooldown_exemptions = tuple(
        decision.symbol
        for decision in symbol_priority_plan.decisions
        if decision.cooldown_exempted
    )
    active_queued = tuple(symbol for symbol in watchlist.active_lifecycle_symbols if symbol in queued_set)
    adaptive_excluded = tuple(
        symbol
        for symbol in requested_symbols
        if symbol_priority_plan.enabled and symbol not in queued_set and symbol not in health_excluded_set
    )
    explicit_excluded = tuple(watchlist.explicit_excluded_symbols)
    priority_candidates = (
        symbol_priority_plan.symbols_to_scan if symbol_priority_plan.enabled else watchlist.symbols
    )
    allowed_membership = set(watchlist.universe.resolved_symbols)
    unexpected_priority_symbols = tuple(
        symbol
        for symbol in priority_candidates
        if watchlist.universe.strict_membership and symbol not in allowed_membership
    )
    membership_boundary_ignored = dedupe_symbols(
        (*watchlist.membership_boundary_ignored_symbols, *unexpected_priority_symbols)
    )
    pre_cap_count = watchlist.pre_cap_symbols_count
    return {
        "universe_requested_count": int(watchlist.universe.requested_size),
        "universe_resolved_count": len(watchlist.universe.resolved_symbols),
        "requested_symbol_count": len(requested_symbols),
        "pre_cap_symbol_count": pre_cap_count if pre_cap_count is not None else len(requested_symbols),
        "explicit_user_excluded_count": len(explicit_excluded),
        "symbol_health_excluded_count": len(health_excluded),
        "lifecycle_priority_additions_count": len(watchlist.lifecycle_priority_added_symbols),
        "lifecycle_priority_promoted_count": len(watchlist.lifecycle_priority_promoted_symbols),
        "lifecycle_priority_dropped_count": len(watchlist.lifecycle_priority_dropped_symbols),
        "active_lifecycle_monitoring_count": len(active_queued),
        "active_lifecycle_cooldown_exemption_count": len(cooldown_exemptions),
        "active_lifecycle_over_cap_count": watchlist.active_lifecycle_over_cap_count,
        "active_lifecycle_exceeded_cap": watchlist.active_lifecycle_over_cap_count > 0,
        "adaptive_priority_enabled": bool(symbol_priority_plan.enabled),
        "adaptive_priority_excluded_count": len(adaptive_excluded),
        "membership_boundary_ignored_count": len(membership_boundary_ignored),
        "lifecycle_membership_ignored_count": len(watchlist.lifecycle_membership_ignored_symbols),
        "queue_cap_applied": bool(watchlist.queue_cap_applied),
        "queue_cap": args.max_symbols,
        "final_queued_count": len(queued),
        "first_resolved_symbols": list(watchlist.universe.resolved_symbols[:20]),
        "first_queued_symbols": list(queued[:20]),
        "exclusion_examples": {
            "explicit_user_excluded": list(explicit_excluded[:20]),
            "symbol_health_cooldown": list(health_excluded[:20]),
            "active_lifecycle_monitoring": list(active_queued[:20]),
            "active_lifecycle_cooldown_exemption": list(cooldown_exemptions[:20]),
            "adaptive_priority_excluded": list(adaptive_excluded[:20]),
            "membership_boundary_ignored": list(membership_boundary_ignored[:20]),
            "lifecycle_outside_membership": list(watchlist.lifecycle_membership_ignored_symbols[:20]),
            "discovery_displaced_by_active_lifecycle": list(watchlist.lifecycle_capacity_displaced_symbols[:20]),
        },
    }


def _should_print_symbol_queue_diagnostics(args: argparse.Namespace) -> bool:
    return bool(args.show_symbol_health or args.diagnostics_level in {"normal", "full"})


def _print_symbol_queue_diagnostics(
    args: argparse.Namespace,
    watchlist: WatchlistResolution,
    symbol_priority_plan: SymbolPriorityPlan,
    queued_symbols: Sequence[str],
) -> None:
    if not _should_print_symbol_queue_diagnostics(args):
        return
    print(_format_symbol_queue_diagnostics(_symbol_queue_diagnostics(args, watchlist, symbol_priority_plan, queued_symbols)))


def _format_symbol_queue_diagnostics(diagnostics: Mapping[str, Any]) -> str:
    examples = diagnostics.get("exclusion_examples")
    example_map = examples if isinstance(examples, Mapping) else {}
    cap_text = "yes" if diagnostics.get("queue_cap_applied") else "no"
    lines = [
        "Symbol queue diagnostics:",
        f"- Universe requested count: {diagnostics.get('universe_requested_count', 0)}",
        f"- Universe resolved count: {diagnostics.get('universe_resolved_count', 0)}",
        f"- Explicit user excluded count: {diagnostics.get('explicit_user_excluded_count', 0)}",
        f"- Symbol health excluded count: {diagnostics.get('symbol_health_excluded_count', 0)}",
        f"- Lifecycle priority additions count: {diagnostics.get('lifecycle_priority_additions_count', 0)}",
        f"- Lifecycle priority promoted count: {diagnostics.get('lifecycle_priority_promoted_count', 0)}",
        f"- Active lifecycle monitoring count: {diagnostics.get('active_lifecycle_monitoring_count', 0)}",
        f"- Active lifecycle cooldown exemptions: {diagnostics.get('active_lifecycle_cooldown_exemption_count', 0)}",
        f"- Active lifecycle over cap count: {diagnostics.get('active_lifecycle_over_cap_count', 0)}",
        f"- Adaptive priority excluded count: {diagnostics.get('adaptive_priority_excluded_count', 0)}",
        f"- Membership boundary ignored count: {diagnostics.get('membership_boundary_ignored_count', 0)}",
        f"- Lifecycle outside membership ignored: {diagnostics.get('lifecycle_membership_ignored_count', 0)}",
        f"- Queue cap applied: {cap_text}",
        f"- Final queued count: {diagnostics.get('final_queued_count', 0)}",
    ]
    for reason in (
        "explicit_user_excluded",
        "symbol_health_cooldown",
        "active_lifecycle_monitoring",
        "active_lifecycle_cooldown_exemption",
        "adaptive_priority_excluded",
        "membership_boundary_ignored",
        "lifecycle_outside_membership",
        "discovery_displaced_by_active_lifecycle",
    ):
        values = example_map.get(reason, ())
        if values:
            lines.append(f"- {reason} examples: {_sequence_text(values)}")
    return "\n".join(lines)


def _apply_symbol_health_if_enabled(
    args: argparse.Namespace,
    result: ScannerRunResult,
    symbol_priority_plan: SymbolPriorityPlan,
) -> ScannerRunResult:
    if not (symbol_priority_plan.enabled or args.show_symbol_health or args.store_scan):
        return result
    try:
        _records, summary = update_symbol_health_for_result(
            args.database_path,
            result,
            plan=symbol_priority_plan,
            cooldown_minutes=args.symbol_cooldown_minutes,
            max_timeout_strikes=args.max_timeout_strikes,
            enabled=symbol_priority_plan.enabled or args.show_symbol_health,
        )
    except StorageError as exc:
        raise SystemExit(str(exc)) from exc
    return result.model_copy(update={"symbol_health": summary})


def _write_history_json(path: Path, payload: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(payload), indent=2, ensure_ascii=False), encoding="utf-8")


def _write_research_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, ensure_ascii=False), encoding="utf-8")


def _command_used(argv: Sequence[str] | None) -> str:
    tokens = list(sys.argv[1:] if argv is None else argv)
    return "run_scan.py " + " ".join(str(token) for token in tokens) if tokens else "run_scan.py"


def _apply_edge_analytics_to_result(
    result: ScannerRunResult,
    edge_report: EdgeAnalyticsReport,
) -> ScannerRunResult:
    updated_results = tuple(
        _apply_edge_analytics_to_symbol(symbol_result, edge_report)
        for symbol_result in result.results
    )
    return result.model_copy(update={"results": updated_results})


def _apply_edge_analytics_to_symbol(
    symbol_result: ScannerSymbolResult,
    edge_report: EdgeAnalyticsReport,
) -> ScannerSymbolResult:
    condition_key = _condition_key_for_symbol_result(symbol_result)
    match = match_historical_condition(edge_report, condition_key)
    return symbol_result.model_copy(
        update={
            "edge_analytics": {
                "enabled": True,
                "min_sample": edge_report.min_sample,
                "condition_key": condition_key.model_dump(mode="json"),
                "top_historical_edges": [
                    item.model_dump(mode="json")
                    for item in edge_report.strongest_conditions[:3]
                ],
                "safety_note": edge_report.safety_note,
            },
            "expectancy_metrics": match.expectancy_metrics.model_dump(mode="json"),
            "confidence_label": match.confidence_label,
            "historical_match_summary": match.model_dump(mode="json"),
        }
    )


def _condition_key_for_symbol_result(symbol_result: ScannerSymbolResult):
    diagnostics = dict(representative_strategy_diagnostics(symbol_result))
    diagnostics.setdefault("value_area_high", symbol_result.value_area_high)
    diagnostics.setdefault("value_area_low", symbol_result.value_area_low)
    mode = _display(diagnostics.get("mode"))
    if mode == NA:
        mode = _first_mode(symbol_result)
    readiness_score = build_symbol_display(symbol_result).readiness_score
    return condition_key_from_diagnostics(
        symbol=symbol_result.symbol,
        mode=mode,
        diagnostics=diagnostics,
        readiness_score=readiness_score,
    )


def _first_mode(symbol_result: ScannerSymbolResult) -> str:
    for values in (symbol_result.valid_strategy_modes, symbol_result.rejected_strategy_modes):
        if values:
            return str(values[0])
    if symbol_result.strategy_diagnostics:
        return str(next(iter(symbol_result.strategy_diagnostics.keys())))
    return NA


def _scanner_runner(cache: MarketDataCache | None, *, log: Any | None = None) -> Any:
    return _scanner_runner_with_context(cache, log=log)


def _scanner_runner_with_context(
    cache: MarketDataCache | None,
    *,
    log: Any | None = None,
    btc_d_context_service: BtcDominanceContextService | None = None,
    microstructure_flow_service: MicrostructureFlowService | None = None,
    liquidation_flow_service: LiquidationFlowService | None = None,
) -> Any:
    try:
        return ScannerRunner(
            market_data_cache=cache,
            btc_d_context_service=btc_d_context_service,
            microstructure_flow_service=microstructure_flow_service,
            liquidation_flow_service=liquidation_flow_service,
            log=log,
        )
    except TypeError:
        try:
            return ScannerRunner(market_data_cache=cache)
        except TypeError:
            return ScannerRunner()


def _btc_d_context_service_for_config(
    config: ScannerRunConfig,
) -> BtcDominanceContextService | None:
    if not config.global_context_enabled or not config.btc_d_context_enabled:
        return None
    return BtcDominanceContextService(
        CoinPaprikaBtcDominanceProvider(
            timeout_seconds=config.btc_d_request_timeout_sec,
        ),
        cache_ttl_seconds=config.btc_d_cache_ttl_sec,
        fresh_seconds=max(
            DEFAULT_BTC_D_FRESH_SECONDS,
            config.btc_d_cache_ttl_sec * 2,
        ),
        max_stale_seconds=max(
            DEFAULT_BTC_D_MAX_STALE_SECONDS,
            config.btc_d_cache_ttl_sec * 12,
        ),
    )


def _microstructure_flow_service_for_config(
    config: ScannerRunConfig,
) -> MicrostructureFlowService | None:
    if not config.microstructure_flow_enabled or config.exchange != "binance":
        return None
    return MicrostructureFlowService(
        stale_after_seconds=config.microstructure_flow_stale_sec,
        max_symbols=config.microstructure_flow_max_symbols,
    )


def _liquidation_flow_service_for_config(
    config: ScannerRunConfig,
) -> LiquidationFlowService | None:
    if not config.liquidation_flow_enabled or config.exchange != "binance":
        return None
    return LiquidationFlowService(
        stale_after_seconds=config.liquidation_flow_stale_sec,
        max_symbols=config.liquidation_flow_max_symbols,
    )


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


async def _run_watch_mode(
    args: argparse.Namespace,
    *,
    watchlist: WatchlistResolution,
    config: ScannerRunConfig,
    diagnostics_level: str,
    display_mode: str,
    effective_candle_limit: int,
    command_used: str,
) -> None:
    btc_d_context_service = _btc_d_context_service_for_config(config)
    microstructure_flow_service = _microstructure_flow_service_for_config(config)
    liquidation_flow_service = _liquidation_flow_service_for_config(config)
    legacy_watch_activation_delivery = _legacy_watch_activation_delivery_enabled(args)
    if legacy_watch_activation_delivery:
        telegram_bot_token, telegram_chat_id, telegram_dry_run = _watch_telegram_credentials(args)
    else:
        telegram_bot_token, telegram_chat_id, telegram_dry_run = None, None, True
    telegram_live = bool(
        legacy_watch_activation_delivery and args.telegram_live_alerts and not telegram_dry_run
    )
    try:
        state = load_watch_state(WATCH_STATE_PATH)
    except WatchModeError as exc:
        raise SystemExit(str(exc)) from exc

    if args.watch_symbols_from_latest_run:
        try:
            state = seed_watch_state_from_run_payload(
                state,
                load_run_payload(LATEST_RUN_PATH),
                watchlist.symbols,
            )
        except WatchModeError as exc:
            raise SystemExit(str(exc)) from exc

    watchlist = _watchlist_with_lifecycle_priority(args, watchlist)
    startup_priority_plan = _symbol_priority_plan_for_watchlist(args, watchlist)
    startup_queued_symbols = _queued_symbols_for_scan(args, watchlist, startup_priority_plan)

    console = ScannerConsolePresenter(mode=_watch_console_mode(args))
    legacy_alert_status = (
        ("live" if telegram_live else "dry-run")
        if legacy_watch_activation_delivery
        else "suppressed (lifecycle setup route selected)"
    )
    startup_output = console.format_watch_startup(
        source_label=watchlist.source_label,
        queued_symbols=len(startup_queued_symbols),
        lifecycle_alerts=_telegram_manual_lifecycle_status_label(args),
        admin_drafts=_telegram_admin_draft_status_label(),
        legacy_alerts=legacy_alert_status,
        warnings=_startup_warnings(args, effective_candle_limit),
    )
    if startup_output:
        console.emit(startup_output)

    iteration = 0
    completed_iterations = 0
    stored_scan_runs = 0
    failure_streak = 0
    if microstructure_flow_service is not None:
        await microstructure_flow_service.start(startup_queued_symbols)
    if liquidation_flow_service is not None:
        await liquidation_flow_service.start(startup_queued_symbols)
    iteration_in_progress = False
    scheduled_start_monotonic = time.monotonic()
    try:
        while True:
            iteration += 1
            iteration_started_at = _watch_iteration_timestamp()
            iteration_started_monotonic = time.monotonic()
            scheduled_start_at = _watch_timestamp_offset(
                iteration_started_at, scheduled_start_monotonic - iteration_started_monotonic
            )
            previous_state = state
            iteration_in_progress = True
            iteration_watchlist, execution, iteration_error = await _attempt_watch_scan_iteration(
                args,
                startup_watchlist=watchlist,
                config=config,
                iteration=iteration,
                console_presenter=console,
                btc_d_context_service=btc_d_context_service,
                microstructure_flow_service=microstructure_flow_service,
                liquidation_flow_service=liquidation_flow_service,
            )
            if iteration_error is not None:
                disposition = classify_watch_exception(iteration_error)
                if disposition == WatchFailureDisposition.RECOVERABLE:
                    failure_streak += 1
                selected_backoff = (
                    failure_backoff_seconds(failure_streak)
                    if disposition == WatchFailureDisposition.RECOVERABLE
                    else 0.0
                )
                finished_monotonic = time.monotonic()
                schedule = schedule_after_iteration(
                    scheduled_start_monotonic=scheduled_start_monotonic,
                    actual_start_monotonic=iteration_started_monotonic,
                    finished_monotonic=finished_monotonic,
                    interval_seconds=args.watch_interval_sec,
                    backoff_seconds=selected_backoff,
                )
                failed_summary = _failed_watch_iteration_summary(
                    iteration=iteration,
                    status="FATAL" if disposition == WatchFailureDisposition.FATAL else "FAILED",
                    watchlist=watchlist,
                    iteration_error=iteration_error,
                    scheduled_start_at=scheduled_start_at,
                    actual_start_at=iteration_started_at,
                    schedule=schedule,
                    failure_streak=failure_streak,
                    selected_backoff=selected_backoff,
                )
                _record_failed_watch_iteration(args, failed_summary, watchlist=watchlist, console_presenter=console)
                iteration_in_progress = False
                completed_iterations = iteration
                if disposition == WatchFailureDisposition.FATAL:
                    raise iteration_error
                if args.watch_max_iterations is not None and iteration >= args.watch_max_iterations:
                    break
                scheduled_start_monotonic = schedule.next_scheduled_monotonic
                await asyncio.sleep(max(0.0, scheduled_start_monotonic - time.monotonic()))
                continue
            assert iteration_watchlist is not None and execution is not None
            completed_at = _watch_iteration_timestamp()
            activations: list[WatchActivation] = []
            updated_state = previous_state
            phase_statuses = dict(execution.phase_statuses)
            iteration_errors = list(execution.recoverable_errors)

            for symbol_result in execution.result.results:
                if symbol_result.iteration_outcome == "not_run":
                    continue
                previous_symbol_state = previous_state.symbols.get(symbol_result.symbol)
                should_alert = legacy_watch_activation_delivery and should_trigger_activation_alert(
                    symbol_result,
                    previous_symbol_state,
                    portfolio_selection=execution.portfolio_selection if args.portfolio_select else None,
                )
                alert_triggered = False
                if should_alert:
                    message = format_watch_activation_alert(symbol_result)
                    delivery = await deliver_watch_activation_alert(
                        message,
                        live=args.telegram_live_alerts,
                        dry_run=telegram_dry_run,
                        telegram_bot_token=telegram_bot_token,
                        telegram_chat_id=telegram_chat_id,
                    )
                    alert_triggered = delivery.status in {"dry_run", "sent"}
                    activation = WatchActivation(
                        symbol=symbol_result.symbol,
                        mode=_watch_activation_mode(symbol_result),
                        message=message,
                        delivery_status=delivery.status,
                        delivery_detail=delivery.detail,
                        integrity_manifest=build_watch_activation_alert_manifest(
                            symbol_result,
                            message=message,
                            delivery_status=delivery.status,
                            live=telegram_live,
                        ),
                    )
                    activations.append(activation)

                updated_state = update_watch_state_for_result(
                    updated_state,
                    symbol_result,
                    alert_triggered=alert_triggered,
                    seen_at=completed_at,
                )

            state = updated_state
            try:
                save_watch_state(
                    WATCH_STATE_PATH, state, expected_updated_at=previous_state.updated_at
                )
            except WatchModeError as exc:
                state = previous_state
                phase_statuses["watch_state"] = "FAILED"
                iteration_errors.append(_watch_phase_error("watch_state", exc))
            else:
                phase_statuses["watch_state"] = "SUCCESS"

            continue_watching = args.watch_max_iterations is None or iteration < args.watch_max_iterations
            iteration_status = _iteration_status_from_phases(phase_statuses)
            if iteration_status == "SUCCESS":
                failure_streak = 0
            else:
                failure_streak += 1
            selected_backoff = (
                failure_backoff_seconds(failure_streak)
                if continue_watching and iteration_status != "SUCCESS"
                else 0.0
            )
            finished_monotonic = time.monotonic()
            completed_at = _watch_iteration_timestamp()
            schedule = schedule_after_iteration(
                scheduled_start_monotonic=scheduled_start_monotonic,
                actual_start_monotonic=iteration_started_monotonic,
                finished_monotonic=finished_monotonic,
                interval_seconds=args.watch_interval_sec,
                backoff_seconds=selected_backoff,
            )
            next_scan_seconds = schedule.sleep_seconds if continue_watching else 0
            runtime_sec = schedule.duration_seconds
            outcome_counts = _queued_symbol_outcome_counts(execution.result, execution.queued_symbols)
            symbol_outcomes = _queued_symbol_outcomes(execution.result, execution.queued_symbols)
            summary = build_watch_iteration_summary(
                iteration=iteration,
                result=execution.result,
                activations=activations,
                next_scan_seconds=next_scan_seconds,
                scanned_at=completed_at,
            )
            summary = summary.model_copy(
                update={
                    "iteration_id": execution.storage_run_id or uuid4().hex,
                    "status": iteration_status,
                    "scheduled_start": scheduled_start_at,
                    "actual_start": iteration_started_at,
                    "finished_at": completed_at,
                    "duration_seconds": schedule.duration_seconds,
                    "sleep_seconds": next_scan_seconds,
                    "cadence_lag_seconds": schedule.cadence_lag_seconds,
                    "overrun_seconds": schedule.overrun_seconds,
                    "missed_interval_count": schedule.missed_interval_count,
                    "consecutive_failure_count": failure_streak,
                    "selected_backoff_seconds": selected_backoff,
                    "next_scheduled_attempt": _watch_timestamp_offset(completed_at, next_scan_seconds),
                    "queue_total": len(execution.queued_symbols),
                    "outcome_counts": outcome_counts,
                    "symbol_outcomes": symbol_outcomes,
                    "phase_statuses": dict(phase_statuses),
                    "errors": tuple(iteration_errors),
                    "active_lifecycle_count": len(iteration_watchlist.active_lifecycle_symbols),
                    "telegram_outbox_status": dict(execution.telegram_outbox_status),
                    "database_storage_status": "NOT_REQUESTED",
                }
            )
            stored_manifest_run_id = execution.storage_run_id
            if args.store_scan:
                try:
                    stored_manifest_run_id = _store_watch_iteration_scan_run(
                        args,
                        execution=execution,
                        summary=summary.model_copy(update={"database_storage_status": "PENDING"}),
                        started_at=iteration_started_at,
                        completed_at=completed_at,
                        runtime_sec=runtime_sec,
                        command_used=command_used,
                    )
                except (OSError, StorageError, SystemExit) as exc:
                    if classify_watch_exception(exc) == WatchFailureDisposition.FATAL:
                        raise
                    phase_statuses["scan_storage"] = "FAILED"
                    iteration_errors.append(_watch_phase_error("scan_storage", exc))
                    database_storage_status = "FAILED"
                else:
                    phase_statuses["scan_storage"] = "SUCCESS"
                    database_storage_status = "SUCCESS"
                    stored_scan_runs += 1
            else:
                phase_statuses["scan_storage"] = "SKIPPED"
                database_storage_status = "NOT_REQUESTED"

            phase_statuses["scan_manifest"] = "SUCCESS"
            status_before_downstream_storage = iteration_status
            iteration_status = _iteration_status_from_phases(phase_statuses)
            if iteration_status == "SUCCESS":
                failure_streak = 0
            elif status_before_downstream_storage == "SUCCESS":
                failure_streak = 1
            selected_backoff = (
                failure_backoff_seconds(failure_streak)
                if continue_watching and iteration_status != "SUCCESS"
                else 0.0
            )
            finished_monotonic = time.monotonic()
            completed_at = _watch_iteration_timestamp()
            schedule = schedule_after_iteration(
                scheduled_start_monotonic=scheduled_start_monotonic,
                actual_start_monotonic=iteration_started_monotonic,
                finished_monotonic=finished_monotonic,
                interval_seconds=args.watch_interval_sec,
                backoff_seconds=selected_backoff,
            )
            next_scan_seconds = schedule.sleep_seconds if continue_watching else 0.0
            summary = summary.model_copy(
                update={
                    "status": iteration_status,
                    "finished_at": completed_at,
                    "duration_seconds": schedule.duration_seconds,
                    "sleep_seconds": next_scan_seconds,
                    "next_scan_seconds": next_scan_seconds,
                    "cadence_lag_seconds": schedule.cadence_lag_seconds,
                    "overrun_seconds": schedule.overrun_seconds,
                    "missed_interval_count": schedule.missed_interval_count,
                    "consecutive_failure_count": failure_streak,
                    "selected_backoff_seconds": selected_backoff,
                    "next_scheduled_attempt": _watch_timestamp_offset(completed_at, next_scan_seconds),
                    "phase_statuses": dict(phase_statuses),
                    "errors": tuple(iteration_errors),
                    "database_storage_status": database_storage_status,
                }
            )
            try:
                manifest_row = _append_scan_run_manifest(
                    execution.result,
                    watchlist=iteration_watchlist,
                    ranked_results=execution.ranked_results,
                    manifest_path=SCAN_RUN_MANIFEST_PATH,
                    nightly_history_path=NIGHTLY_SCAN_HISTORY_PATH,
                    run_id=stored_manifest_run_id,
                    output_scan_path=args.output_json,
                    latest_scan_path=args.save_run,
                    watch_iteration=summary.iteration,
                    watch_summary=summary,
                )
            except OSError as exc:
                phase_statuses["scan_manifest"] = "FAILED"
                iteration_errors.append(_watch_phase_error("scan_manifest", exc))
                if iteration_status == "SUCCESS":
                    failure_streak = 1
                iteration_status = _iteration_status_from_phases(phase_statuses)
                selected_backoff = failure_backoff_seconds(failure_streak) if continue_watching else 0.0
                schedule = schedule_after_iteration(
                    scheduled_start_monotonic=scheduled_start_monotonic,
                    actual_start_monotonic=iteration_started_monotonic,
                    finished_monotonic=time.monotonic(),
                    interval_seconds=args.watch_interval_sec,
                    backoff_seconds=selected_backoff,
                )
                next_scan_seconds = schedule.sleep_seconds if continue_watching else 0.0
                summary = summary.model_copy(
                    update={
                        "status": iteration_status,
                        "sleep_seconds": next_scan_seconds,
                        "next_scan_seconds": next_scan_seconds,
                        "consecutive_failure_count": failure_streak,
                        "selected_backoff_seconds": selected_backoff,
                        "next_scheduled_attempt": _watch_timestamp_offset(_watch_iteration_timestamp(), next_scan_seconds),
                        "phase_statuses": dict(phase_statuses),
                        "errors": tuple(iteration_errors),
                    }
                )
                manifest_row = _scan_run_manifest_row(
                    execution.result,
                    watchlist=iteration_watchlist,
                    ranked_results=execution.ranked_results,
                    run_id=stored_manifest_run_id,
                    watch_iteration=summary.iteration,
                    output_scan_path=args.output_json,
                    latest_scan_path=args.save_run,
                )
                manifest_row["watch_supervisor"] = summary.model_dump(mode="json")

            presentation_summary = summary
            if args.watch_output_file is not None:
                try:
                    append_watch_output(args.watch_output_file, summary)
                except OSError as exc:
                    phase_statuses["watch_output"] = "FAILED"
                    iteration_errors.append(_watch_phase_error("watch_output", exc))
                    presentation_summary = summary.model_copy(
                        update={
                            "phase_statuses": dict(phase_statuses),
                            "errors": tuple(iteration_errors),
                        }
                    )
                else:
                    phase_statuses["watch_output"] = "SUCCESS"
            await _route_admin_report(
                execution.result,
                ranked_results=execution.ranked_results,
                manifest_row=manifest_row,
                console_presenter=console,
            )
            console.emit(
                console.format_watch_iteration(
                    presentation_summary,
                    results=execution.result.results,
                    telegram_deliveries=tuple(
                        getattr(execution.telegram_delivery_summary, "deliveries", ()) or ()
                    ),
                )
            )
            iteration_in_progress = False
            completed_iterations = iteration

            if not continue_watching:
                break
            scheduled_start_monotonic = schedule.next_scheduled_monotonic
            await asyncio.sleep(max(0.0, scheduled_start_monotonic - time.monotonic()))
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        if iteration_in_progress:
            cancelled_watchlist = getattr(exc, "watch_iteration_watchlist", watchlist)
            cancelled_summary = _cancelled_watch_iteration_summary(
                iteration=iteration,
                watchlist=cancelled_watchlist,
                queued_symbols=tuple(getattr(exc, "watch_queue_symbols", ())),
                completed_results=tuple(getattr(exc, "watch_completed_results", ())),
                scheduled_start_at=scheduled_start_at,
                actual_start_at=iteration_started_at,
                actual_start_monotonic=iteration_started_monotonic,
                scheduled_start_monotonic=scheduled_start_monotonic,
                interval_seconds=args.watch_interval_sec,
                interruption=exc,
            )
            _record_failed_watch_iteration(args, cancelled_summary, watchlist=cancelled_watchlist, console_presenter=console)
            completed_iterations = iteration
        _print_watch_shutdown(
            completed_iterations=completed_iterations,
            stored_scan_runs=stored_scan_runs,
            database_path=args.database_path,
            console_presenter=console,
        )
        return
    finally:
        if microstructure_flow_service is not None:
            await microstructure_flow_service.stop()
        if liquidation_flow_service is not None:
            await liquidation_flow_service.stop()


async def _attempt_watch_scan_iteration(
    args: argparse.Namespace,
    *,
    startup_watchlist: WatchlistResolution,
    config: ScannerRunConfig,
    iteration: int,
    console_presenter: ScannerConsolePresenter | None = None,
    btc_d_context_service: BtcDominanceContextService | None = None,
    microstructure_flow_service: MicrostructureFlowService | None = None,
    liquidation_flow_service: LiquidationFlowService | None = None,
) -> tuple[WatchlistResolution | None, WatchScanExecution | None, Exception | SystemExit | None]:
    try:
        watchlist = await _watchlist_for_watch_iteration(args, startup_watchlist)
        execution = await _run_watch_scan_iteration(
            args,
            watchlist=watchlist,
            config=config,
            iteration=iteration,
            console_presenter=console_presenter,
            btc_d_context_service=btc_d_context_service,
            microstructure_flow_service=microstructure_flow_service,
            liquidation_flow_service=liquidation_flow_service,
        )
    except (Exception, SystemExit) as exc:
        return None, None, exc
    return watchlist, execution, None


def _iteration_status_from_phases(phase_statuses: Mapping[str, str]) -> str:
    normalized = {key: str(value).upper() for key, value in phase_statuses.items()}
    if any(normalized.get(key) == "FAILED" for key in ("universe", "queue", "scanner")):
        return WatchIterationStatus.FAILED.value
    if any(value in {"FAILED", "PARTIAL"} for value in normalized.values()):
        return WatchIterationStatus.PARTIAL.value
    return WatchIterationStatus.SUCCESS.value


def _watch_timestamp_offset(timestamp: str, seconds: float) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return NA
    return (parsed + timedelta(seconds=float(seconds))).replace(microsecond=0).isoformat()


def _cancelled_watch_iteration_summary(
    *,
    iteration: int,
    watchlist: WatchlistResolution,
    queued_symbols: tuple[str, ...],
    completed_results: tuple[ScannerSymbolResult, ...],
    scheduled_start_at: str,
    actual_start_at: str,
    actual_start_monotonic: float,
    scheduled_start_monotonic: float,
    interval_seconds: float,
    interruption: KeyboardInterrupt | asyncio.CancelledError,
) -> WatchIterationSummary:
    finished_monotonic = time.monotonic()
    decision = schedule_after_iteration(
        scheduled_start_monotonic=scheduled_start_monotonic,
        actual_start_monotonic=actual_start_monotonic,
        finished_monotonic=finished_monotonic,
        interval_seconds=interval_seconds,
    )
    queued_set = set(queued_symbols)
    completed_by_symbol = {
        result.symbol: result
        for result in completed_results
        if result.symbol in queued_set
    }
    raw_counts = Counter(
        result.iteration_outcome or "errored"
        for result in completed_by_symbol.values()
    )
    raw_counts["not_run"] += len(queued_symbols) - len(completed_by_symbol)
    outcome_counts = {
        key: int(raw_counts.get(key, 0))
        for key in ("evaluated", "rejected", "errored", "timed_out", "not_run")
    }
    symbol_outcomes: dict[str, dict[str, str]] = {}
    for symbol in queued_symbols:
        result = completed_by_symbol.get(symbol)
        if result is None:
            symbol_outcomes[symbol] = {
                "outcome": "not_run",
                "reason_code": "cancelled_not_run",
                "status": ScannerPipelineStatus.NOT_RUN.value,
            }
            continue
        outcome = str(result.iteration_outcome)
        if outcome == "not_run":
            reason_code = result.not_run_reason
        elif outcome == "timed_out":
            reason_code = result.timeout_status
        elif outcome == "errored":
            reason_code = "scan_error"
        elif outcome == "rejected":
            reason_code = (
                result.rejection_stage
                if result.rejection_stage != NA
                else "scanner_gate_rejection"
            )
        else:
            reason_code = "evaluated"
        symbol_outcomes[symbol] = {
            "outcome": outcome,
            "reason_code": reason_code,
            "status": getattr(result.status, "value", str(result.status)),
        }
    finished_at = _watch_iteration_timestamp()
    return WatchIterationSummary(
        iteration=iteration,
        scanned_at=finished_at,
        symbols_watched=len(watchlist.symbols),
        valid_activations=0,
        still_watching=0,
        rejected_no_edge=outcome_counts["rejected"],
        data_issues=outcome_counts["errored"] + outcome_counts["timed_out"],
        iteration_id=uuid4().hex,
        status=WatchIterationStatus.CANCELLED.value,
        scheduled_start=scheduled_start_at,
        actual_start=actual_start_at,
        finished_at=finished_at,
        duration_seconds=decision.duration_seconds,
        sleep_seconds=0.0,
        cadence_lag_seconds=decision.cadence_lag_seconds,
        overrun_seconds=decision.overrun_seconds,
        missed_interval_count=decision.missed_interval_count,
        next_scheduled_attempt=NA,
        queue_total=len(queued_symbols),
        outcome_counts=outcome_counts,
        symbol_outcomes=symbol_outcomes,
        phase_statuses={"scanner": "CANCELLED"},
        errors=(_watch_phase_error("iteration", interruption),),
        active_lifecycle_count=len(watchlist.active_lifecycle_symbols),
        database_storage_status="NOT_ATTEMPTED",
        next_scan_seconds=None,
    )


def _failed_watch_iteration_summary(
    *,
    iteration: int,
    status: str,
    watchlist: WatchlistResolution,
    iteration_error: Exception | SystemExit,
    scheduled_start_at: str,
    actual_start_at: str,
    schedule: Any,
    failure_streak: int,
    selected_backoff: float,
) -> WatchIterationSummary:
    finished_at = _watch_iteration_timestamp()
    error_text = _watch_phase_error("iteration", iteration_error)
    return WatchIterationSummary(
        iteration=iteration,
        scanned_at=finished_at,
        symbols_watched=len(watchlist.symbols),
        valid_activations=0,
        still_watching=0,
        rejected_no_edge=0,
        data_issues=0,
        iteration_id=uuid4().hex,
        status=status,
        scheduled_start=scheduled_start_at,
        actual_start=actual_start_at,
        finished_at=finished_at,
        duration_seconds=schedule.duration_seconds,
        sleep_seconds=schedule.sleep_seconds,
        cadence_lag_seconds=schedule.cadence_lag_seconds,
        overrun_seconds=schedule.overrun_seconds,
        missed_interval_count=schedule.missed_interval_count,
        consecutive_failure_count=failure_streak,
        selected_backoff_seconds=selected_backoff,
        next_scheduled_attempt=_watch_timestamp_offset(finished_at, schedule.sleep_seconds),
        queue_total=0,
        outcome_counts={
            "evaluated": 0,
            "rejected": 0,
            "errored": 0,
            "timed_out": 0,
            "not_run": 0,
        },
        phase_statuses={"iteration": status},
        errors=(error_text,),
        active_lifecycle_count=len(watchlist.active_lifecycle_symbols),
        database_storage_status="NOT_ATTEMPTED",
        next_scan_seconds=schedule.sleep_seconds,
    )


def _record_failed_watch_iteration(
    args: argparse.Namespace,
    summary: WatchIterationSummary,
    *,
    watchlist: WatchlistResolution,
    console_presenter: ScannerConsolePresenter | None = None,
) -> None:
    if console_presenter is not None:
        console_presenter.emit(console_presenter.format_watch_iteration(summary))
    else:
        print(format_watch_iteration_summary(summary))
        print(f"Iteration error: {summary.errors[0]}")
    if args.watch_output_file is not None:
        try:
            append_watch_output(args.watch_output_file, summary)
        except OSError as exc:
            print(f"Warning: watch output persistence failed safely: {type(exc).__name__}: {exc}")

    row = {
        "run_id": summary.iteration_id,
        "timestamp": summary.finished_at,
        "watch_iteration": summary.iteration,
        "status": summary.status,
        "universe_mode": _display(getattr(watchlist.universe, "mode", NA)),
        "universe_label": _display(getattr(watchlist.universe, "label", watchlist.source_label)),
        "symbols_scanned": 0,
        "watch_supervisor": summary.model_dump(mode="json"),
    }
    try:
        SCAN_RUN_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SCAN_RUN_MANIFEST_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
            handle.write("\n")
        _write_nightly_scan_history(NIGHTLY_SCAN_HISTORY_PATH, row)
    except OSError as exc:
        print(f"Warning: failed iteration manifest persistence failed safely: {type(exc).__name__}: {exc}")


async def _watchlist_for_watch_iteration(
    args: argparse.Namespace,
    startup_watchlist: WatchlistResolution,
) -> WatchlistResolution:
    if _watch_mode_refreshes_universe(args):
        watchlist = await _resolve_watchlist_for_scan(args)
    else:
        watchlist = startup_watchlist
    return _watchlist_with_lifecycle_priority(args, watchlist)


def _watch_mode_refreshes_universe(args: argparse.Namespace) -> bool:
    return bool(
        args.universe != MANUAL_UNIVERSE_MODE
        and not args.watch_symbols_from_latest_run
        and not args.watch_only_near_misses
    )


async def _run_watch_scan_iteration(
    args: argparse.Namespace,
    *,
    watchlist: WatchlistResolution,
    config: ScannerRunConfig,
    iteration: int,
    console_presenter: ScannerConsolePresenter | None = None,
    btc_d_context_service: BtcDominanceContextService | None = None,
    microstructure_flow_service: MicrostructureFlowService | None = None,
    liquidation_flow_service: LiquidationFlowService | None = None,
) -> WatchScanExecution:
    phase_statuses = {"universe": "SUCCESS"}
    recoverable_errors: list[str] = []
    telegram_outbox_status: dict[str, int] = {}
    telegram_delivery_summary: TelegramLifecycleDeliverySummary | None = None
    cache = (
        MarketDataCache(enabled=True, ttl_seconds=args.cache_ttl_seconds, file_path=args.cache_file)
        if args.cache_enabled
        else None
    )
    iteration_config = ScannerRunConfig.model_validate({**config.model_dump(), "symbols": list(watchlist.symbols)})
    latest_results_by_symbol: dict[str, ScannerSymbolResult] = {}
    symbol_priority_plan = _symbol_priority_plan_for_watchlist(args, watchlist)
    queued_symbols = _queued_symbols_for_scan(args, watchlist, symbol_priority_plan)
    phase_statuses["queue"] = "SUCCESS"
    if microstructure_flow_service is not None:
        try:
            await microstructure_flow_service.reconcile_symbols(queued_symbols)
        except Exception as exc:
            phase_statuses["microstructure_flow"] = "PARTIAL"
            recoverable_errors.append(_watch_phase_error("microstructure_flow", exc))
        else:
            phase_statuses["microstructure_flow"] = "SUCCESS"
    if liquidation_flow_service is not None:
        try:
            await liquidation_flow_service.reconcile_symbols(queued_symbols)
        except Exception as exc:
            phase_statuses["liquidation_flow"] = "PARTIAL"
            recoverable_errors.append(_watch_phase_error("liquidation_flow", exc))
        else:
            phase_statuses["liquidation_flow"] = "SUCCESS"
    symbol_queue_diagnostics = _symbol_queue_diagnostics(args, watchlist, symbol_priority_plan, queued_symbols)
    scan_config = (
        ScannerRunConfig.model_validate({**iteration_config.model_dump(), "symbols": list(queued_symbols)})
        if queued_symbols
        else iteration_config
    )
    resume_state = ResumeState(results_by_symbol={}, skipped_symbols=(), loaded_symbols=())
    scan_run_id = uuid4().hex
    resume_metadata = {
        **_resume_metadata(
            args,
            watchlist.symbols,
            resume_state,
            queued_symbols,
            watchlist.universe,
            symbol_queue_diagnostics=symbol_queue_diagnostics,
        ),
        "run_id": scan_run_id,
        "scan_run_id": scan_run_id,
        "watch_mode": True,
        "watch_iteration": iteration,
    }

    async def after_symbol(symbol_result: ScannerSymbolResult, completed: int, total: int) -> None:
        latest_results_by_symbol[symbol_result.symbol] = symbol_result
        if args.progress and (console_presenter is None or console_presenter.mode == "verbose"):
            if console_presenter is None:
                print(_progress_line(symbol_result, completed=completed, total=total))
            else:
                console_presenter.emit(_progress_line(symbol_result, completed=completed, total=total))
        if (
            args.save_run is not None
            and phase_statuses.get("checkpoint_output") != "FAILED"
        ):
            partial_result = _combined_run_result(
                config=iteration_config,
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
                market_regime=None,
            )
            try:
                _write_run_json(args.save_run, partial_result)
            except OSError as exc:
                phase_statuses["checkpoint_output"] = "FAILED"
                recoverable_errors.append(_watch_phase_error("checkpoint_output", exc))

    async def progress(message: str) -> None:
        if console_presenter is None:
            print(message, flush=True)
        else:
            console_presenter.emit(message)

    if queued_symbols:
        runner = _scanner_runner_with_context(
            cache,
            log=console_presenter.scanner_logger() if console_presenter is not None else None,
            btc_d_context_service=btc_d_context_service,
            microstructure_flow_service=microstructure_flow_service,
            liquidation_flow_service=liquidation_flow_service,
        )
        try:
            scan_result = await _run_scanner(
                runner,
                scan_config,
                after_symbol=after_symbol if (args.progress or args.save_run is not None) else None,
                progress=progress if args.progress and (console_presenter is None or console_presenter.mode == "verbose") else None,
                resume_metadata=resume_metadata,
            )
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            completed_results = tuple(
                getattr(exc, "scanner_completed_results", ())
            ) or tuple(latest_results_by_symbol.values())
            setattr(exc, "watch_queue_symbols", queued_symbols)
            setattr(exc, "watch_completed_results", completed_results)
            setattr(exc, "watch_iteration_watchlist", watchlist)
            raise
        for symbol_result in scan_result.results:
            latest_results_by_symbol[symbol_result.symbol] = symbol_result
        missing_reason = (
            "global_timeout_not_run"
            if scan_result.runtime_stats.global_timeout_hit
            else "scanner_result_missing_not_run"
        )
        for symbol in queued_symbols:
            if symbol not in latest_results_by_symbol:
                latest_results_by_symbol[symbol] = _not_run_symbol_result(symbol, reason=missing_reason)

        result = _combined_run_result(
            config=iteration_config,
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
            market_regime=scan_result.market_regime,
            global_context=scan_result.global_context,
        )
    else:
        result = _combined_run_result(
            config=iteration_config,
            watchlist_symbols=watchlist.symbols,
            results_by_symbol=latest_results_by_symbol,
            cache=cache,
            retry_diagnostics=(),
            resume_metadata={**resume_metadata, "pending_symbols": list(watchlist.symbols)},
            runtime_stats=None,
            market_regime=None,
        )
    storage_run_id = scan_run_id
    phase_statuses["scanner"] = _scanner_phase_status(result, queued_symbols)
    if _lifecycle_enabled(args):
        try:
            result = _apply_lifecycle_if_enabled(args, result, scan_run_id=storage_run_id)
        except (Exception, SystemExit) as exc:
            if classify_watch_exception(exc) == WatchFailureDisposition.FATAL:
                raise
            phase_statuses["lifecycle"] = "FAILED"
            recoverable_errors.append(_watch_phase_error("lifecycle", exc))
        else:
            lifecycle_summary = result.scanner_process_summary
            phase_statuses["lifecycle"] = str(lifecycle_summary.get("status", "SUCCESS"))
            for item in lifecycle_summary.get("errors", ()):
                if isinstance(item, Mapping):
                    recoverable_errors.append(
                        f"lifecycle:{item.get('symbol', NA)}:{item.get('detail', NA)}"
                    )
    else:
        phase_statuses["lifecycle"] = "SKIPPED"
    if _telegram_lifecycle_public_delivery_enabled(args):
        try:
            telegram_summary = await _deliver_telegram_manual_signals_if_enabled(
                args,
                result,
                scan_run_id=storage_run_id,
                print_summary=False,
            )
        except (Exception, SystemExit) as exc:
            if classify_watch_exception(exc) == WatchFailureDisposition.FATAL:
                raise
            phase_statuses["telegram_outbox"] = "FAILED"
            recoverable_errors.append(_watch_phase_error("telegram_outbox", exc))
        else:
            telegram_delivery_summary = telegram_summary
            telegram_outbox_status = _telegram_outbox_status_summary(telegram_summary)
            phase_statuses["telegram_outbox"] = _telegram_outbox_phase_status(telegram_outbox_status)
    else:
        phase_statuses["telegram_outbox"] = "SKIPPED"
    symbol_health_enabled = bool(
        symbol_priority_plan.enabled or args.show_symbol_health or args.store_scan
    )
    try:
        result = _apply_symbol_health_if_enabled(args, result, symbol_priority_plan)
    except (Exception, SystemExit) as exc:
        if classify_watch_exception(exc) == WatchFailureDisposition.FATAL:
            raise
        phase_statuses["symbol_health"] = "FAILED"
        recoverable_errors.append(_watch_phase_error("symbol_health", exc))
    else:
        phase_statuses["symbol_health"] = "SUCCESS" if symbol_health_enabled else "SKIPPED"
    outcome_counts = _queued_symbol_outcome_counts(result, queued_symbols)
    result = result.model_copy(
        update={
            "resume_metadata": {
                **result.resume_metadata,
                "watch_phase_statuses": dict(phase_statuses),
                "watch_recoverable_errors": list(recoverable_errors),
                "telegram_outbox_status": dict(telegram_outbox_status),
                "queue_outcome_counts": dict(outcome_counts),
            }
        }
    )
    ranked_results = rank_scan_results(result.results, rank_results=args.rank_results)
    portfolio_selection = _portfolio_selection_for_result(args, result) if args.portfolio_select else None
    if portfolio_selection is not None:
        ranked_results = _ranked_results_with_selected_first(ranked_results, portfolio_selection)

    output_requested = args.output_json is not None or args.save_run is not None
    try:
        if args.output_json is not None:
            _write_run_json(args.output_json, result, ranked_results=ranked_results, portfolio_selection=portfolio_selection)
        if args.save_run is not None:
            _write_run_json(args.save_run, result, ranked_results=ranked_results, portfolio_selection=portfolio_selection)
    except (Exception, SystemExit) as exc:
        if classify_watch_exception(exc) == WatchFailureDisposition.FATAL:
            raise
        phase_statuses["output_files"] = "FAILED"
        recoverable_errors.append(_watch_phase_error("output_files", exc))
    else:
        phase_statuses["output_files"] = "SUCCESS" if output_requested else "SKIPPED"

    result = result.model_copy(
        update={
            "resume_metadata": {
                **result.resume_metadata,
                "watch_phase_statuses": dict(phase_statuses),
                "watch_recoverable_errors": list(recoverable_errors),
            }
        }
    )
    return WatchScanExecution(
        result=result,
        ranked_results=ranked_results,
        portfolio_selection=portfolio_selection,
        symbol_priority_plan=symbol_priority_plan,
        queued_symbols=queued_symbols,
        storage_run_id=storage_run_id,
        phase_statuses=phase_statuses,
        recoverable_errors=tuple(recoverable_errors),
        telegram_outbox_status=telegram_outbox_status,
        telegram_delivery_summary=telegram_delivery_summary,
    )


def _store_watch_iteration_scan_run(
    args: argparse.Namespace,
    *,
    execution: WatchScanExecution,
    summary: WatchIterationSummary,
    started_at: str,
    completed_at: str,
    runtime_sec: float,
    command_used: str,
) -> str:
    raw_payload = _json_payload(
        execution.result,
        ranked_results=execution.ranked_results,
        portfolio_selection=execution.portfolio_selection,
    )
    raw_payload["watch_iteration"] = summary.model_dump(mode="json")
    raw_payload["watch_iteration_storage"] = {
        "watch_iteration_number": summary.iteration,
        "started_at": started_at,
        "completed_at": completed_at,
        "symbols_requested": summary.symbols_watched,
        "symbols_queued": len(execution.queued_symbols),
        "symbols_completed": execution.result.runtime_stats.completed_symbols,
        "valid_activations": summary.valid_activations,
        "still_watching": summary.still_watching,
        "rejected_no_edge": summary.rejected_no_edge,
        "data_issues": summary.data_issues,
        "runtime_sec": runtime_sec,
        "market_regime": _display(execution.result.market_regime.state.value),
    }
    metadata = WatchIterationMetadata(
        iteration_number=summary.iteration,
        started_at=started_at,
        completed_at=completed_at,
        symbols_requested=summary.symbols_watched,
        symbols_queued=len(execution.queued_symbols),
        symbols_completed=execution.result.runtime_stats.completed_symbols,
        valid_activations=summary.valid_activations,
        still_watching=summary.still_watching,
        rejected_no_edge=summary.rejected_no_edge,
        data_issues=summary.data_issues,
        runtime_sec=runtime_sec,
        portfolio_summary=_watch_portfolio_summary(execution.portfolio_selection),
        symbol_health_summary=_watch_symbol_health_summary(execution.result),
    )
    try:
        return store_scan_result(
            args.database_path,
            execution.result,
            ranked_results=execution.ranked_results,
            portfolio_selection=execution.portfolio_selection,
            command_preset=args.command_preset,
            command_used=command_used,
            raw_payload=raw_payload,
            run_id=execution.storage_run_id,
            watch_iteration=metadata,
        )
    except StorageError as exc:
        raise SystemExit(str(exc)) from exc


def _watch_portfolio_summary(portfolio_selection: PortfolioSelectionResult | None) -> dict[str, Any]:
    if portfolio_selection is None:
        return {}
    return {
        "selected_count": portfolio_selection.selected_count,
        "total_risk_pct": portfolio_selection.total_risk_pct,
        "selected_symbols": list(selected_symbols(portfolio_selection)),
        "rejected_count": len(portfolio_selection.rejected_candidates),
        "rejected_due_to_correlation": portfolio_selection.rejected_due_to_correlation,
        "rejected_due_to_risk_limit": portfolio_selection.rejected_due_to_risk_limit,
        "portfolio_warnings": list(portfolio_selection.portfolio_warnings),
    }


def _watch_symbol_health_summary(result: ScannerRunResult) -> dict[str, Any]:
    return dict(result.symbol_health) if isinstance(result.symbol_health, Mapping) else {}


def _print_watch_shutdown(
    *,
    completed_iterations: int,
    stored_scan_runs: int,
    database_path: Path | str,
    console_presenter: ScannerConsolePresenter | None = None,
) -> None:
    if console_presenter is not None:
        console_presenter.emit(
            console_presenter.format_watch_shutdown(
                completed_iterations=completed_iterations,
                stored_scan_runs=stored_scan_runs,
                database_path=_database_path_text(database_path),
            )
        )
        return
    print("Watch mode stopped by user.")
    print(f"Completed iterations: {completed_iterations}")
    print(f"Stored scan runs: {stored_scan_runs}")
    print(f"Data saved to: {_database_path_text(database_path)}")


def _database_path_text(database_path: Path | str) -> str:
    return Path(database_path).as_posix()


def _watch_telegram_credentials(args: argparse.Namespace) -> tuple[str | None, str | None, bool]:
    if not args.telegram_live_alerts:
        return None, None, True
    settings = Settings()
    dry_run = bool(settings.telegram_dry_run)
    bot_token = (settings.telegram_bot_token or "").strip()
    chat_id = (settings.telegram_chat_id or "").strip()
    if not dry_run and (not bot_token or not chat_id):
        raise SystemExit(
            "--telegram-live-alerts true requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env. "
            "No live Telegram alert was sent."
        )
    return bot_token or None, chat_id or None, dry_run


def _watch_iteration_timestamp() -> str:
    from app.watch_mode import now_utc_iso

    return now_utc_iso()


def _watch_activation_mode(symbol_result: ScannerSymbolResult) -> str:
    if symbol_result.valid_strategy_modes:
        return symbol_result.valid_strategy_modes[0]
    trade_idea = symbol_result.trade_idea
    setup_type = _display(getattr(trade_idea, "setup_type", NA))
    for mode in ("challenge", "swing", "scalp"):
        if setup_type.endswith(f"_{mode}") or mode in setup_type:
            return mode
    return NA


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
        structure_timeframe=args.structure_timeframe,
        replay_candles=replay_candles,
        same_candle_policy=args.same_candle_policy,
        max_hold_candles=args.replay_max_hold_candles,
        max_fill_candles=args.replay_max_fill_candles,
        max_setups=args.backtest_max_setups,
        edge_min_sample=args.edge_min_sample,
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


def _portfolio_selection_for_result(
    args: argparse.Namespace,
    result: ScannerRunResult,
) -> PortfolioSelectionResult:
    limits = PortfolioRiskLimits(
        max_selected_setups=args.max_selected_setups,
        max_portfolio_risk_pct=args.max_portfolio_risk_pct,
        max_beta_group_risk_pct=args.max_beta_group_risk_pct,
        allow_correlated_setups=args.allow_correlated_setups,
    )
    return build_portfolio_selection_from_scan(result, risk_limits=limits)


def _ranked_results_with_selected_first(
    ranked_results: Sequence[Any],
    portfolio_selection: PortfolioSelectionResult,
) -> tuple[Any, ...]:
    selected_order = {symbol: index for index, symbol in enumerate(selected_symbols(portfolio_selection))}
    ordered = sorted(
        ranked_results,
        key=lambda item: (
            0 if item.symbol_result.symbol in selected_order else 1,
            selected_order.get(item.symbol_result.symbol, item.display_rank),
            item.display_rank,
        ),
    )
    return tuple(
        item.__class__(
            symbol_result=item.symbol_result,
            display=item.display,
            display_rank=index + 1,
            original_index=item.original_index,
        )
        for index, item in enumerate(ordered)
    )


def _top_setup_result(ranked_results: Sequence[Any]) -> ScannerSymbolResult | None:
    for ranked in ranked_results:
        if ranked.display.display_bucket == "valid":
            return ranked.symbol_result
    return None


def _update_continue_watch_state(args: argparse.Namespace, result: ScannerRunResult) -> tuple[str, ...]:
    try:
        state = load_watch_state(WATCH_STATE_PATH)
    except WatchModeError as exc:
        raise SystemExit(str(exc)) from exc

    timestamp = _watch_iteration_timestamp()
    promoted: list[str] = []
    updated_state = state
    continued_symbols = set(getattr(args, "continued_watch_symbols", ()))

    for symbol_result in result.results:
        display = build_symbol_display(symbol_result)
        previous = updated_state.symbols.get(symbol_result.symbol)
        should_track = (
            display.display_bucket == "near_miss"
            or symbol_result.symbol in continued_symbols
            or previous is not None
        )
        if not should_track:
            continue
        if display.display_bucket == "near_miss" and symbol_result.symbol not in promoted:
            if previous is None or not previous.invalidated:
                promoted.append(symbol_result.symbol)
        updated_state = update_watch_state_for_result(
            updated_state,
            symbol_result,
            alert_triggered=False,
            seen_at=timestamp,
        )

    try:
        save_watch_state(WATCH_STATE_PATH, updated_state)
    except WatchModeError as exc:
        raise SystemExit(str(exc)) from exc
    return tuple(promoted)


def _watch_candidate_symbols(result: ScannerRunResult) -> tuple[str, ...]:
    symbols: list[str] = []
    for symbol_result in result.results:
        lifecycle = symbol_result.lifecycle_state
        if lifecycle is not None and lifecycle.current_state.value in {"STALKING", "TRIGGERED", "CONFIRMED", "WATCHLISTED"}:
            symbols.append(symbol_result.symbol)
            continue
        display = build_symbol_display(symbol_result)
        if display.display_bucket == "near_miss" or display.readiness_label in {"HOT WATCH", "WATCH"}:
            symbols.append(symbol_result.symbol)
    return tuple(symbols)


def _replay_warning_lines(replay_summary: ReplaySummary | None) -> tuple[str, ...]:
    if replay_summary is None:
        return ()
    warnings: list[str] = []
    if _display(replay_summary.sample_size_warning) != NA:
        warnings.append(f"replay sample size: {replay_summary.sample_size_warning}")
    for symbol in replay_summary.symbols:
        warning = _display(symbol.sample_size_warning)
        if warning != NA and warning not in warnings:
            warnings.append(f"{symbol.symbol}: {warning}")
    return tuple(warnings)


def _json_payload(
    result: ScannerRunResult,
    ranked_results=None,
    *,
    replay_summary: ReplaySummary | None = None,
    portfolio_selection: PortfolioSelectionResult | None = None,
) -> dict[str, object]:
    payload = result.model_dump(mode="json")
    run_id = _result_run_id(result)
    if run_id != NA:
        payload["run_id"] = run_id
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
    if portfolio_selection is not None:
        payload["portfolio_selection"] = portfolio_selection.model_dump(mode="json")
        payload["selected_candidates"] = [
            candidate.model_dump(mode="json") for candidate in portfolio_selection.selected_candidates
        ]
        payload["rejected_candidates"] = [
            candidate.model_dump(mode="json") for candidate in portfolio_selection.rejected_candidates
        ]
        payload["exposure_summary"] = [
            exposure.model_dump(mode="json") for exposure in portfolio_selection.exposure_summary
        ]
        payload["portfolio_warnings"] = list(portfolio_selection.portfolio_warnings)
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
            format_pullback_intelligence_block(symbol_result),
            "Setup quality:",
            _format_setup_quality_diagnostics(symbol_result),
            "Historical edge analytics:",
            _format_historical_match(symbol_result),
            "Strategy diagnostics:",
            _format_strategy_diagnostics(symbol_result),
        )
    )


def _format_regime_details(result: ScannerRunResult) -> str:
    regime = result.market_regime
    lines = [
        "Market Climate Details",
        f"State: {_display(regime.state.value)}",
        f"Confidence: {_display(regime.confidence_score)}",
        f"Band: {_display(regime.confidence_band.value)}",
        f"Risk: {_display(regime.risk_level.value)}",
        f"Strictness: {_display(regime.strictness.value)}",
        f"Notes: {_sequence_text(regime.environment_notes)}",
        f"Boosts: {_sequence_text(regime.boosts)}",
        f"Penalties: {_sequence_text(regime.penalties)}",
    ]
    for mode in ("challenge", "swing", "scalp"):
        compatibility = regime.compatibility_scores.get(mode)
        if compatibility is None:
            continue
        lines.append(
            (
                f"{mode}: {compatibility.label} {compatibility.score}/100 "
                f"vol={compatibility.volatility_suitability} "
                f"trend={compatibility.trend_suitability} "
                f"execution={compatibility.execution_quality_suitability} "
                f"allowed={_bool_text(compatibility.allowed)}"
            )
        )
    return "\n".join(lines)


def _format_run_diagnostics(result: ScannerRunResult) -> str:
    cache_stats = result.cache_stats or {}
    runtime = result.runtime_stats
    process_memory = runtime.process_memory
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
            "Process RSS diagnostics:",
            f"Measurement status: {process_memory.measurement_status}",
            f"Source: {process_memory.source}",
            f"RSS start bytes: {process_memory.rss_start_bytes}",
            f"RSS end bytes: {process_memory.rss_end_bytes}",
            f"RSS observed peak bytes: {process_memory.rss_observed_peak_bytes}",
            f"RSS delta bytes: {process_memory.rss_delta_bytes}",
            (
                f"Samples: {process_memory.samples_succeeded}/"
                f"{process_memory.samples_attempted} succeeded"
            ),
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


def _format_historical_match(symbol_result: ScannerSymbolResult) -> str:
    summary = symbol_result.historical_match_summary
    if not isinstance(summary, Mapping) or not summary:
        return NA
    metrics = summary.get("expectancy_metrics")
    if not isinstance(metrics, Mapping):
        metrics = {}
    return "\n".join(
        (
            f"Matched: {_display(summary.get('matched'))}",
            f"Confidence: {_display(summary.get('confidence_label'))}",
            f"Sample size: {_display(summary.get('matching_sample_size'))}",
            f"Expectancy: {_display(metrics.get('expectancy'))}",
            f"TP1 hit rate: {_percentage_display(metrics.get('tp1_hit_rate'))}",
            f"TP2 hit rate: {_percentage_display(metrics.get('tp2_hit_rate'))}",
            f"Warning: {_display(summary.get('warning'))}",
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
                f"{confirmation_timeframe} confirmation={_display(diagnostics.get('candles_5m_count') if confirmation_timeframe == '5m' else diagnostics.get('candles_15m_count'))}",
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
                f"{mode} configured global RR: {_display(diagnostics.get('configured_global_minimum_rr'))}",
                f"{mode} hard RR floor: {_display(diagnostics.get('hard_mode_floor'))}",
                f"{mode} effective RR threshold: {_display(diagnostics.get('effective_minimum_rr'))}",
                f"{mode} candidate RR: {_display(diagnostics.get('candidate_rr'))}",
                f"{mode} RR rejection reason: {_display(diagnostics.get('rr_rejection_reason'))}",
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


def _cli_database_path_from_argv(argv: Sequence[str]) -> Path:
    for index, token in enumerate(argv):
        if token == "--database-path" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if token.startswith("--database-path="):
            return Path(token.split("=", 1)[1])
    return DEFAULT_DATABASE_PATH


def cli_main() -> int:
    _configure_cli_encoding()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if "--watch" in sys.argv[1:]:
            _print_watch_shutdown(
                completed_iterations=0,
                stored_scan_runs=0,
                database_path=_cli_database_path_from_argv(sys.argv[1:]),
            )
            return 0
        print("Stopped by user.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
