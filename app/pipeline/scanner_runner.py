from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.alert_agent import AlertAgent, AlertResult
from app.agents.derivatives_orderflow import DerivativesOrderflowAgent, DerivativesOrderflowResult
from app.agents.journal_agent import JournalAgent, JournalEntryResult, JournalStatus
from app.agents.risk_manager import RiskDecision, RiskManagerAgent, RiskManagerInput
from app.agents.technical_structure import (
    TechnicalAnalysisStatus,
    TechnicalStructureAgent,
    TechnicalStructureResult,
)
from app.agents.trade_idea import TradeIdeaAgent, TradeIdeaResult
from app.analytics.derivatives_enrichment import (
    DerivativesEnrichmentInput,
    DerivativesEnrichmentResult,
    enrich_derivatives,
)
from app.analytics.near_miss_intelligence import NearMissIntelligence, build_near_miss_intelligence
from app.analytics.pullback_intelligence import PullbackIntelligenceResult, build_pullback_intelligence
from app.analytics.market_regime import (
    MarketRegimeInput,
    MarketRegimeResult,
    RegimeAdjustment,
    RegimeRiskLevel,
    RegimeState,
    RegimeStrictness,
    default_market_regime_result,
    disabled_market_regime_result,
    evaluate_market_regime,
)
from app.analytics.setup_quality import (
    SetupQualityGrade,
    SetupQualityInput,
    SetupQualityResult,
    SetupQualityState,
    default_setup_quality_result,
    validate_setup_quality,
)
from app.analytics.target_intelligence import (
    TargetFailureType,
    TargetIntelligenceResult,
    TargetQualityGrade,
    build_target_intelligence,
)
from app.analytics.volume_profile import VolumeProfileInput, VolumeProfileResult, calculate_volume_profile
from app.cache.market_data_cache import CachedMarketDataClient, MarketDataCache
from app.context import (
    BtcDominanceContextService,
    CoinPaprikaBtcDominanceProvider,
    ContextValue,
    GlobalContextSnapshot,
    build_global_context_snapshot,
    build_internal_btc_context,
    build_weekend_context,
)
from app.context.models import ContextStatus
from app.context.btc import BTC_CONTEXT_TIMEFRAMES
from app.context.btc_d import (
    DEFAULT_BTC_D_CACHE_TTL_SECONDS,
    DEFAULT_BTC_D_FRESH_SECONDS,
    DEFAULT_BTC_D_MAX_STALE_SECONDS,
    DEFAULT_BTC_D_REQUEST_TIMEOUT_SECONDS,
)
from app.core.process_memory import ProcessMemoryReading, read_process_rss
from app.data.candle_integrity import (
    CandleIntegrityError,
    closed_candles_as_of,
    normalize_utc_timestamp,
)
from app.data.dtos import NA, CandleDTO, FundingDTO, MaybeDecimal, MaybeInt, OpenInterestDTO, TickerDTO
from app.data.exceptions import ExchangeTimeoutError
from app.data.exchange_clients import BaseExchangeClient, BinanceFuturesClient, BybitLinearClient
from app.data.timeframes import resample_ohlcv_candles
from app.core.minimum_rr import (
    DEFAULT_CONFIGURED_MINIMUM_RR,
    hard_mode_minimum_rr,
    validate_configured_minimum_rr,
)
from app.core.trade_plan_integrity import TradePlanIntegrityResult, validate_trade_plan
from app.lifecycle.models import (
    SetupLifecycleOutcomeProgress,
    SetupLifecycleRecord,
    SetupTransitionResult,
)
from app.microstructure.models import MicrostructureFlowSnapshot
from app.microstructure.service import MicrostructureFlowService
from app.scoring.opportunity_scoring import OpportunityScoreResult, OpportunityScoringEngine
from app.strategies.liquidity_grab_pullback import (
    DEFAULT_CONFIRMATION_TIMEFRAME,
    DEFAULT_STRUCTURE_TIMEFRAME,
    LiquidityGrabEngine,
    LiquidityGrabInput,
    LiquidityGrabMode,
    LiquidityGrabResult,
    LiquidityGrabSetup,
)

logger = logging.getLogger(__name__)

OUTPUT_QUANT = Decimal("0.00000001")
MIN_DERIVATIVES_SCORE = Decimal("40")
LIQUIDITY_GRAB_STRATEGY_NAME = "liquidity_grab_pullback"
DEFAULT_STRATEGY_MODES = (
    LiquidityGrabMode.challenge,
    LiquidityGrabMode.swing,
    LiquidityGrabMode.scalp,
)
DIRECT_STRATEGY_TIMEFRAMES = ("12h", "4h", "1h", "15m")
SYNTHETIC_2D_SOURCE_TIMEFRAME = "1d"
NO_VALID_STRATEGY_SETUP_REASON = "No valid Liquidity-Grab Pullback setup."
MIN_12H_VOLUME_PROFILE_CANDLES = 20
MIN_STRATEGY_CLOSED_CANDLES = 20
MIN_REGIME_CLOSED_CANDLES = 40
BINANCE_KLINE_LIMIT_MIN = 1
BINANCE_KLINE_LIMIT_MAX = 1500
DEFAULT_REQUEST_TIMEOUT_SEC = 10.0
DEFAULT_SYMBOL_TIMEOUT_SEC = 60.0
DEFAULT_REPLAY_CANDLES = 300
SAFE_REPLAY_CANDLE_LIMIT_MAX = 500
FAST_CANDLE_LIMIT = 220
FAST_REPLAY_CANDLES = 240
FAST_OPTIONAL_REQUEST_TIMEOUT_SEC = 0.5
TARGET_INTEGRITY_FAILED_GATE = "target_integrity"
INVALID_TP_SEQUENCE_WARNING = "Invalid TP sequence: target labels are not monotonic by reward distance."
TARGET_FAILURE_SEVERITY_FATAL = "fatal_target_failure"
TARGET_FAILURE_SEVERITY_SOFT = "soft_target_warning"
TARGET_FAILURE_SEVERITY_PASSED = "target_passed"
TARGET_INTEGRITY_BLOCKING_FAILURE_TYPES = {
    TargetFailureType.OPPOSING_STRUCTURE_BLOCK.value,
    TargetFailureType.DATA_INCOMPLETE.value,
    "RR_COMPRESSED",
    "NO_CLEAN_TARGET_PATH",
}


class ScannerPipelineStatus(str, Enum):
    NOT_RUN = "not_run"
    SCAN_ERROR = "scan_error"
    SCANNED_NO_SETUP = "scanned_no_setup"
    REJECTED_BY_TECHNICAL = "rejected_by_technical"
    REJECTED_BY_DERIVATIVES = "rejected_by_derivatives"
    REJECTED_BY_RISK = "rejected_by_risk"
    REJECTED_BY_SCORING = "rejected_by_scoring"
    REJECTED_BY_REGIME = "rejected_by_regime"
    IDEA_CREATED = "idea_created"
    ALERT_DRY_RUN_CREATED = "alert_dry_run_created"
    JOURNAL_ENTRY_CREATED = "journal_entry_created"
    FAILED = "failed"


class ScannerSymbolConfig(BaseModel):
    symbol: str

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized


class ScannerRiskConfig(BaseModel):
    account_equity: Decimal
    risk_per_trade_pct: Decimal
    max_daily_risk_pct: Decimal | None = None
    current_daily_loss_pct: Decimal | None = None
    leverage: Decimal | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator(
        "account_equity",
        "risk_per_trade_pct",
        "max_daily_risk_pct",
        "current_daily_loss_pct",
        "leverage",
        mode="before",
    )
    @classmethod
    def _normalize_decimal(cls, value: Any) -> Any:
        if value is None:
            return None
        return _decimal_from(value, "risk_config")


class ScannerRunConfig(BaseModel):
    symbols: tuple[ScannerSymbolConfig, ...]
    exchange: Literal["binance", "bybit"]
    interval: str = "15m"
    candle_limit: int = 250
    replay_candles: int | None = None
    dry_run_alerts: bool = True
    account_equity: Decimal
    risk_per_trade_pct: Decimal
    max_daily_risk_pct: Decimal | None = None
    current_daily_loss_pct: Decimal | None = None
    leverage: Decimal | None = None
    min_score_for_idea: Decimal = Decimal("80")
    min_rr: Decimal = DEFAULT_CONFIGURED_MINIMUM_RR
    verbose: bool = False
    strategy_name: str | None = LIQUIDITY_GRAB_STRATEGY_NAME
    strategy_modes: tuple[LiquidityGrabMode, ...] = DEFAULT_STRATEGY_MODES
    enable_strategy_output: bool = True
    include_formatted_strategy_output: bool = True
    aggressive_toggle: bool = False
    htf_timeframe: str = "2d"
    bias_timeframe: str = "12h"
    structure_timeframe: str = DEFAULT_STRUCTURE_TIMEFRAME
    execution_timeframe: str = "15m"
    confirmation_timeframe: str = DEFAULT_CONFIRMATION_TIMEFRAME
    cache_enabled: bool = True
    cache_ttl_seconds: int | None = None
    cache_file: Path | None = None
    request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC
    symbol_timeout_sec: float = DEFAULT_SYMBOL_TIMEOUT_SEC
    scan_timeout_sec: float | None = None
    fast_mode: bool = False
    market_regime_enabled: bool = False
    regime_risk_mode: Literal["conservative", "balanced", "aggressive"] = "balanced"
    regime_strictness: Literal["low", "normal", "high"] = "normal"
    global_context_enabled: bool = False
    btc_context_enabled: bool = True
    btc_d_context_enabled: bool = True
    btc_d_cache_ttl_sec: int = DEFAULT_BTC_D_CACHE_TTL_SECONDS
    btc_d_request_timeout_sec: float = DEFAULT_BTC_D_REQUEST_TIMEOUT_SECONDS
    microstructure_flow_enabled: bool = False
    microstructure_flow_stale_sec: float = 5.0
    microstructure_flow_max_symbols: int = 100
    decision_timestamp: datetime | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("symbols", mode="before")
    @classmethod
    def _normalize_symbols(cls, value: Any) -> Any:
        if isinstance(value, str):
            return ({"symbol": value},)
        if isinstance(value, Sequence):
            return tuple({"symbol": item} if isinstance(item, str) else item for item in value)
        return value

    @field_validator(
        "account_equity",
        "risk_per_trade_pct",
        "max_daily_risk_pct",
        "current_daily_loss_pct",
        "leverage",
        "min_score_for_idea",
        mode="before",
    )
    @classmethod
    def _normalize_decimal(cls, value: Any) -> Any:
        if value is None:
            return None
        return _decimal_from(value, "scanner_run_config")

    @field_validator("min_rr", mode="before")
    @classmethod
    def _normalize_min_rr(cls, value: Any) -> Decimal:
        return validate_configured_minimum_rr(value)

    @field_validator("strategy_name")
    @classmethod
    def _normalize_strategy_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if normalized != LIQUIDITY_GRAB_STRATEGY_NAME:
            raise ValueError(f"unsupported strategy_name: {value!r}")
        return normalized

    @field_validator("strategy_modes", mode="before")
    @classmethod
    def _normalize_strategy_modes(cls, value: Any) -> Any:
        if value is None:
            return DEFAULT_STRATEGY_MODES
        if isinstance(value, str):
            return (value,)
        if isinstance(value, Sequence):
            return tuple(value)
        return value

    @field_validator("interval", "htf_timeframe", "bias_timeframe", "structure_timeframe", "execution_timeframe", "confirmation_timeframe")
    @classmethod
    def _interval_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("interval must not be blank")
        return normalized

    @field_validator("candle_limit")
    @classmethod
    def _candle_limit_in_range(cls, value: int) -> int:
        if value < 1:
            raise ValueError("candle_limit must be at least 1")
        return value

    @field_validator("replay_candles")
    @classmethod
    def _replay_candles_in_range(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("replay_candles must be at least 1")
        return value

    @field_validator("cache_ttl_seconds")
    @classmethod
    def _cache_ttl_in_range(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("cache_ttl_seconds must be zero or greater")
        return value

    @field_validator("btc_d_cache_ttl_sec")
    @classmethod
    def _btc_d_cache_ttl_in_range(cls, value: int) -> int:
        if value < 0:
            raise ValueError("btc_d_cache_ttl_sec must be zero or greater")
        return value

    @field_validator("microstructure_flow_max_symbols")
    @classmethod
    def _microstructure_max_symbols_in_range(cls, value: int) -> int:
        if value < 1 or value > 1024:
            raise ValueError("microstructure_flow_max_symbols must be between 1 and 1024")
        return value

    @field_validator(
        "request_timeout_sec",
        "symbol_timeout_sec",
        "scan_timeout_sec",
        "btc_d_request_timeout_sec",
        "microstructure_flow_stale_sec",
        mode="before",
    )
    @classmethod
    def _timeout_positive(cls, value: Any) -> Any:
        if value is None:
            return None
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout values must be numbers") from exc
        if normalized <= 0:
            raise ValueError("timeout values must be greater than zero")
        return normalized

    @field_validator("decision_timestamp", mode="before")
    @classmethod
    def _normalize_decision_timestamp(cls, value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        return normalize_utc_timestamp(value, field_name="decision_timestamp")

    @model_validator(mode="after")
    def _validate_config(self) -> ScannerRunConfig:
        if not self.symbols:
            raise ValueError("symbols must include at least one symbol")
        if self.account_equity <= 0:
            raise ValueError("account_equity must be greater than zero")
        if self.risk_per_trade_pct <= 0:
            raise ValueError("risk_per_trade_pct must be greater than zero")
        if self.min_score_for_idea < 0 or self.min_score_for_idea > 100:
            raise ValueError("min_score_for_idea must be between 0 and 100")
        if self.enable_strategy_output and self.strategy_name is not None and not self.strategy_modes:
            raise ValueError("strategy_modes must include at least one mode when strategy output is enabled")
        return self

    @property
    def risk_config(self) -> ScannerRiskConfig:
        return ScannerRiskConfig(
            account_equity=self.account_equity,
            risk_per_trade_pct=self.risk_per_trade_pct,
            max_daily_risk_pct=self.max_daily_risk_pct,
            current_daily_loss_pct=self.current_daily_loss_pct,
            leverage=self.leverage,
        )


class ScannerSymbolResult(BaseModel):
    symbol: str
    status: ScannerPipelineStatus
    status_history: tuple[ScannerPipelineStatus, ...]
    error_message: str | None = None
    rejection_reason: str | None = None
    runtime_seconds: float | None = None
    timed_out: bool = False
    timeout_status: Literal["none", "request_timeout", "symbol_timeout", "global_timeout"] = "none"
    iteration_outcome: Literal[
        "evaluated", "rejected", "errored", "timed_out", "not_run"
    ] | None = None
    not_run_reason: str = NA
    candle_count: int = 0
    current_price: MaybeDecimal = NA
    funding_rate: MaybeDecimal = NA
    open_interest: MaybeDecimal = NA
    candles_fetched: int = 0
    latest_close: MaybeDecimal = NA
    latest_high: MaybeDecimal = NA
    latest_low: MaybeDecimal = NA
    technical_score: MaybeInt = NA
    technical_status: str = NA
    technical_timeframe: str = NA
    technical_required_bars: MaybeInt = NA
    technical_available_bars: MaybeInt = NA
    derivatives_score: MaybeInt = NA
    trend_context: str = NA
    recent_range_high: MaybeDecimal = NA
    recent_range_low: MaybeDecimal = NA
    nearest_support: MaybeDecimal = NA
    nearest_resistance: MaybeDecimal = NA
    latest_swing_high: MaybeDecimal = NA
    latest_swing_low: MaybeDecimal = NA
    sweep_detected: bool = False
    bos_detected: bool = False
    choch_detected: bool = False
    funding_direction: str = NA
    funding_severity: str = NA
    funding_status: str = NA
    funding_extreme: bool | Literal["N/A"] = NA
    oi_direction: str = NA
    open_interest_change_pct: MaybeDecimal = NA
    price_oi_relationship: str = NA
    price_direction: str = NA
    long_short_ratio: MaybeDecimal = NA
    crowding_risk: str = NA
    squeeze_risk: str = NA
    derivatives_missing_data: tuple[str, ...] = ()
    derivatives_unverified_data: tuple[str, ...] = ()
    derivatives_warnings: tuple[str, ...] = ()
    rejection_stage: str = NA
    rejection_reasons: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()
    strategy_name: str = NA
    strategy_results: dict[str, LiquidityGrabResult] = Field(default_factory=dict)
    formatted_strategy_output: str = NA
    strategy_diagnostics: dict[str, Any] = Field(default_factory=dict)
    valid_strategy_modes: tuple[str, ...] = ()
    rejected_strategy_modes: tuple[str, ...] = ()
    strategy_missing_data: tuple[str, ...] = ()
    strategy_unverified_data: tuple[str, ...] = ()
    volume_profile: VolumeProfileResult | None = None
    volume_profile_12h: VolumeProfileResult | None = None
    volume_profile_source: str = NA
    poc: MaybeDecimal = NA
    value_area_high: MaybeDecimal = NA
    value_area_low: MaybeDecimal = NA
    nearest_high_volume_node: MaybeDecimal = NA
    nearest_low_volume_node: MaybeDecimal = NA
    volume_profile_warnings: tuple[str, ...] = ()
    technical_result: TechnicalStructureResult | None = None
    derivatives_result: DerivativesOrderflowResult | None = None
    derivatives_enrichment: DerivativesEnrichmentResult | None = None
    microstructure_flow: MicrostructureFlowSnapshot | None = None
    risk_decision: RiskDecision | None = None
    score_result: OpportunityScoreResult | None = None
    trade_idea: TradeIdeaResult | None = None
    alert_result: AlertResult | None = None
    journal_entry: JournalEntryResult | None = None
    near_miss_intelligence: NearMissIntelligence | None = None
    pullback_intelligence: PullbackIntelligenceResult | None = None
    target_intelligence: TargetIntelligenceResult | None = None
    setup_quality: SetupQualityResult = Field(default_factory=default_setup_quality_result)
    candidate_quality_grade: str = NA
    final_quality_grade: str = NA
    final_failed_gate: str = NA
    final_block_reason: str = NA
    target_integrity_status: str = NA
    target_failure: str = NA
    target_failure_severity: str = NA
    target_warning_reason: str = NA
    actionability_state: str = NA
    regime_warnings: tuple[str, ...] = ()
    regime_state: str = NA
    regime_confidence_score: MaybeInt = NA
    regime_compatibility_score: MaybeInt = NA
    regime_compatibility_label: str = NA
    regime_penalty: int = Field(default=0, ge=0, le=100)
    regime_blocked: bool = False
    regime_notes: tuple[str, ...] = ()
    regime_diagnostics: dict[str, Any] = Field(default_factory=dict)
    edge_analytics: dict[str, Any] = Field(default_factory=dict)
    expectancy_metrics: dict[str, Any] = Field(default_factory=dict)
    confidence_label: str = NA
    historical_match_summary: dict[str, Any] = Field(default_factory=dict)
    performance_memory: dict[str, Any] = Field(default_factory=dict)
    historical_expectancy: MaybeDecimal = NA
    confidence_bucket: str = NA
    memory_adjustments: dict[str, Any] = Field(default_factory=dict)
    historical_warning: str = NA
    lifecycle_state: SetupLifecycleRecord | None = None
    lifecycle_transition: SetupTransitionResult | None = None
    lifecycle_transitions: tuple[SetupTransitionResult, ...] = Field(
        default=(),
        exclude=True,
        repr=False,
    )
    lifecycle_outcome_progress: SetupLifecycleOutcomeProgress | None = None
    lifecycle_execution_candles: tuple[Any, ...] | None = Field(default=None, exclude=True, repr=False)
    lifecycle_execution_timeframe: str = Field(default=NA, exclude=True)
    lifecycle_decision_timestamp: datetime | None = Field(default=None, exclude=True)


    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def _derive_iteration_outcome(self) -> ScannerSymbolResult:
        outcome = self.iteration_outcome
        if outcome is None:
            if self.status == ScannerPipelineStatus.NOT_RUN:
                outcome = "not_run"
            elif self.timed_out or self.timeout_status != "none":
                outcome = "timed_out"
            elif self.status in (ScannerPipelineStatus.SCAN_ERROR, ScannerPipelineStatus.FAILED):
                outcome = "errored"
            elif self.status in (
                ScannerPipelineStatus.SCANNED_NO_SETUP,
                ScannerPipelineStatus.REJECTED_BY_TECHNICAL,
                ScannerPipelineStatus.REJECTED_BY_DERIVATIVES,
                ScannerPipelineStatus.REJECTED_BY_RISK,
                ScannerPipelineStatus.REJECTED_BY_SCORING,
                ScannerPipelineStatus.REJECTED_BY_REGIME,
            ):
                outcome = "rejected"
            else:
                outcome = "evaluated"
            object.__setattr__(self, "iteration_outcome", outcome)
        if outcome == "not_run" and self.not_run_reason == NA:
            raise ValueError("NOT_RUN results require an explicit not_run_reason")
        return self


class ScannerProcessMemoryStats(BaseModel):
    measurement_status: Literal["Verified", "Unverified", "N/A"] = NA
    source: str = NA
    rss_start_bytes: MaybeInt = NA
    rss_end_bytes: MaybeInt = NA
    rss_observed_peak_bytes: MaybeInt = NA
    rss_delta_bytes: MaybeInt = NA
    samples_attempted: int = 0
    samples_succeeded: int = 0
    samples_failed: int = 0
    failure_codes: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class ScannerRuntimeStats(BaseModel):
    total_runtime_seconds: float = 0.0
    average_seconds_per_symbol: float = 0.0
    slowest_symbol: str = NA
    slowest_symbol_seconds: float = 0.0
    timeout_count: int = 0
    completed_symbols: int = 0
    skipped_symbols: int = 0
    errored_symbols: int = 0
    skipped_errored_symbols: int = 0
    global_timeout_hit: bool = False
    queued_symbols: int = 0
    evaluated_symbols: int = 0
    rejected_symbols: int = 0
    timed_out_symbols: int = 0
    not_run_symbols: int = 0
    outcome_counts: dict[str, int] = Field(default_factory=dict)
    process_memory: ScannerProcessMemoryStats = Field(default_factory=ScannerProcessMemoryStats)

    model_config = ConfigDict(frozen=True)


class ScannerRunResult(BaseModel):
    config: ScannerRunConfig
    results: tuple[ScannerSymbolResult, ...]
    scanned_symbols: int
    failed_symbols: int
    trade_ideas_created: int
    dry_run_alerts_created: int
    journal_entries_created: int
    cache_stats: dict[str, Any] = Field(default_factory=dict)
    retry_diagnostics: tuple[dict[str, Any], ...] = ()
    resume_metadata: dict[str, Any] = Field(default_factory=dict)
    runtime_stats: ScannerRuntimeStats = Field(default_factory=ScannerRuntimeStats)
    market_regime: MarketRegimeResult = Field(default_factory=default_market_regime_result)
    regime_adjustments: RegimeAdjustment = Field(default_factory=lambda: default_market_regime_result().adjustment)
    regime_warnings: tuple[str, ...] = ()
    performance_memory_summary: dict[str, Any] = Field(default_factory=dict)
    symbol_health: dict[str, Any] = Field(default_factory=dict)
    scanner_process_summary: dict[str, Any] = Field(default_factory=dict)
    global_context: GlobalContextSnapshot | None = None

    model_config = ConfigDict(frozen=True)


class _CandidateSetup(BaseModel):
    symbol: str
    exchange: str
    direction: Literal["long", "short"]
    timeframe: str
    setup_type: str
    entry_price: Decimal
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    take_profit_targets: tuple[Decimal, Decimal, Decimal]
    invalidation: str
    cancel_condition: str
    setup_location: Literal["edge", "middle", "breakout_retest", "unknown"]
    technical_summary: str
    confirmed_facts: tuple[str, ...]

    model_config = ConfigDict(frozen=True)


class _CandidateBuildResult(BaseModel):
    candidate: _CandidateSetup | None = None
    status: ScannerPipelineStatus
    reason: str | None = None
    missing_data: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class _OptionalMarketData(BaseModel):
    ticker: TickerDTO | Any | None = None
    funding: FundingDTO | Any | None = None
    funding_history: Sequence[FundingDTO | Any] | None = None
    open_interest: OpenInterestDTO | Any | None = None
    open_interest_history: Sequence[OpenInterestDTO | Any] | None = None
    previous_open_interest: MaybeDecimal = NA
    long_short_ratio: MaybeDecimal = NA
    liquidation_data: Any | None = None
    warnings: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class _StrategyExecution(BaseModel):
    strategy_name: str = NA
    strategy_results: dict[str, LiquidityGrabResult] = Field(default_factory=dict)
    formatted_strategy_output: str = NA
    strategy_diagnostics: dict[str, Any] = Field(default_factory=dict)
    valid_strategy_modes: tuple[str, ...] = ()
    rejected_strategy_modes: tuple[str, ...] = ()
    strategy_missing_data: tuple[str, ...] = ()
    strategy_unverified_data: tuple[str, ...] = ()
    volume_profile: VolumeProfileResult | None = None
    volume_profile_12h: VolumeProfileResult | None = None
    selected_setup: LiquidityGrabSetup | None = None
    pullback_intelligence: PullbackIntelligenceResult | None = None
    target_intelligence: TargetIntelligenceResult | None = None
    microstructure_flow: MicrostructureFlowSnapshot | None = None
    execution_candles: tuple[Any, ...] = Field(default=(), exclude=True, repr=False)
    execution_timeframe: str = NA
    decision_timestamp: datetime | None = None

    model_config = ConfigDict(frozen=True)


class _TargetIntegrityDecision(BaseModel):
    blocked: bool = False
    reason: str = NA
    warning: str = NA
    strategy_execution: _StrategyExecution | None = None

    model_config = ConfigDict(frozen=True)


class ScannerRunner:
    """Connect existing read-only analysis modules into one dry-run scanner flow.

    The runner reads public market data through the configured exchange client,
    creates conditional trade ideas only after deterministic gates pass, and never
    places orders or uses private exchange endpoints.
    """

    def __init__(
        self,
        *,
        exchange_client: BaseExchangeClient | None = None,
        technical_agent: TechnicalStructureAgent | None = None,
        derivatives_agent: DerivativesOrderflowAgent | None = None,
        risk_manager: RiskManagerAgent | None = None,
        scoring_engine: OpportunityScoringEngine | None = None,
        strategy_engine: LiquidityGrabEngine | None = None,
        trade_idea_agent: TradeIdeaAgent | None = None,
        alert_agent: AlertAgent | None = None,
        journal_agent: JournalAgent | None = None,
        market_data_cache: MarketDataCache | None = None,
        btc_d_context_service: BtcDominanceContextService | None = None,
        microstructure_flow_service: MicrostructureFlowService | None = None,
        clock: Callable[[], datetime] | None = None,
        process_memory_sampler: Callable[[], ProcessMemoryReading] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.exchange_client = exchange_client
        self.technical_agent = technical_agent or TechnicalStructureAgent()
        self.derivatives_agent = derivatives_agent or DerivativesOrderflowAgent()
        self.risk_manager = risk_manager or RiskManagerAgent()
        self.scoring_engine = scoring_engine or OpportunityScoringEngine()
        self.strategy_engine = strategy_engine or LiquidityGrabEngine()
        self.trade_idea_agent = trade_idea_agent or TradeIdeaAgent()
        self.alert_agent = alert_agent or AlertAgent()
        self.journal_agent = journal_agent or JournalAgent()
        self.market_data_cache = market_data_cache
        self.btc_d_context_service = btc_d_context_service
        self.microstructure_flow_service = microstructure_flow_service
        self.clock = clock or (lambda: datetime.now(UTC))
        self.process_memory_sampler = process_memory_sampler or read_process_rss
        self.logger = log or logger

    async def run(
        self,
        config: ScannerRunConfig | Mapping[str, Any],
        *,
        after_symbol: Callable[[ScannerSymbolResult, int, int], Any] | None = None,
        progress: Callable[[str], Any] | None = None,
        resume_metadata: Mapping[str, Any] | None = None,
    ) -> ScannerRunResult:
        run_config = config if isinstance(config, ScannerRunConfig) else ScannerRunConfig.model_validate(config)
        if run_config.decision_timestamp is None:
            run_config = run_config.model_copy(
                update={
                    "decision_timestamp": normalize_utc_timestamp(
                        self.clock(),
                        field_name="decision_timestamp",
                    )
                }
            )
        process_memory_readings: list[ProcessMemoryReading] = [
            _safe_process_memory_reading(self.process_memory_sampler)
        ]
        client, owns_client = self._exchange_client_for(run_config)
        global_context: GlobalContextSnapshot | None = None
        results: list[ScannerSymbolResult] = []
        total_symbols = len(run_config.symbols)
        scan_started = time.monotonic()
        global_timeout_hit = False
        market_regime_context = {
            "btc_candles": (), "eth_candles": (), "missing_data": ("market_regime: N/A",)
        }
        scan_deadline = (
            scan_started + run_config.scan_timeout_sec
            if run_config.scan_timeout_sec is not None
            else None
        )

        try:
            if run_config.global_context_enabled:
                global_context = await self._build_global_context(
                    client,
                    run_config,
                    progress=progress,
                )
            if run_config.market_regime_enabled:
                market_regime_context = await self._fetch_market_regime_context(
                    client, run_config, progress=progress
                )
            for symbol_config in run_config.symbols:
                if scan_deadline is not None and time.monotonic() >= scan_deadline:
                    global_timeout_hit = True
                    self.logger.warning("Full scan timeout reached before symbol=%s.", symbol_config.symbol)
                    break

                symbol_started = time.monotonic()
                stop_after_symbol = False
                await _emit_progress(progress, f"Starting {symbol_config.symbol}...")
                symbol_timeout = run_config.symbol_timeout_sec
                symbol_limited_by_scan_deadline = False
                if scan_deadline is not None:
                    remaining_scan_seconds = scan_deadline - time.monotonic()
                    if remaining_scan_seconds <= 0:
                        global_timeout_hit = True
                        self.logger.warning("Full scan timeout reached before symbol=%s.", symbol_config.symbol)
                        break
                    symbol_limited_by_scan_deadline = remaining_scan_seconds < symbol_timeout
                    symbol_timeout = min(symbol_timeout, remaining_scan_seconds)

                try:
                    symbol_result = await asyncio.wait_for(
                        self._scan_symbol(
                            symbol_config,
                            run_config,
                            client,
                            global_context=global_context,
                            progress=progress,
                        ),
                        timeout=symbol_timeout,
                    )
                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - symbol_started
                    if symbol_limited_by_scan_deadline:
                        reason = f"full scan timeout exceeded after {_format_seconds(run_config.scan_timeout_sec)} seconds"
                        timeout_status: Literal["symbol_timeout", "global_timeout"] = "global_timeout"
                        global_timeout_hit = True
                        stop_after_symbol = True
                    else:
                        reason = f"symbol timeout exceeded after {_format_seconds(run_config.symbol_timeout_sec)} seconds"
                        timeout_status = "symbol_timeout"
                    self.logger.error("Scanner timed out for symbol=%s after %.2fs: %s", symbol_config.symbol, elapsed, reason)
                    symbol_result = _scan_error_result(
                        symbol_config.symbol,
                        reason,
                        runtime_seconds=elapsed,
                        timeout_status=timeout_status,
                    )
                except Exception as exc:
                    elapsed = time.monotonic() - symbol_started
                    reason = _clean_error_message(exc)
                    self.logger.error("Scanner failed for symbol=%s: %s", symbol_config.symbol, reason)
                    symbol_result = _scan_error_result(
                        symbol_config.symbol,
                        reason,
                        runtime_seconds=elapsed,
                        timeout_status="request_timeout" if isinstance(exc, ExchangeTimeoutError) else "none",
                    )
                symbol_elapsed = time.monotonic() - symbol_started
                symbol_result = _with_symbol_runtime(
                    symbol_result,
                    runtime_seconds=symbol_elapsed,
                    timeout_status=symbol_result.timeout_status,
                )
                results.append(symbol_result)
                if scan_deadline is not None and time.monotonic() >= scan_deadline and not stop_after_symbol:
                    global_timeout_hit = True
                    stop_after_symbol = True
                    self.logger.warning("Full scan timeout reached after symbol=%s.", symbol_config.symbol)
                await _emit_progress(
                    progress,
                    f"Done {symbol_config.symbol} in {_format_seconds(symbol_elapsed)} seconds.",
                )
                if after_symbol is not None:
                    await _maybe_await(after_symbol(symbol_result, len(results), total_symbols))
                process_memory_readings.append(
                    _safe_process_memory_reading(self.process_memory_sampler)
                )
                if stop_after_symbol:
                    break
            if global_timeout_hit and len(results) < total_symbols:
                completed_symbols = {result.symbol for result in results}
                for symbol_config in run_config.symbols:
                    if symbol_config.symbol in completed_symbols:
                        continue
                    not_run_result = _not_run_result(symbol_config.symbol, reason="global_timeout_not_run")
                    results.append(not_run_result)
                    if after_symbol is not None:
                        await _maybe_await(after_symbol(not_run_result, len(results), total_symbols))
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            setattr(exc, "scanner_completed_results", tuple(results))
            raise
        finally:
            if owns_client and hasattr(client, "aclose"):
                await _maybe_await(client.aclose())

        total_runtime_seconds = time.monotonic() - scan_started
        if (
            scan_deadline is not None
            and len(results) < total_symbols
            and time.monotonic() >= scan_deadline
        ):
            global_timeout_hit = True

        market_regime = _market_regime_for_scan(
            run_config,
            results,
            market_regime_context=market_regime_context,
        )
        adjusted_results = _apply_market_regime_to_results(results, market_regime)
        process_memory_readings.append(
            _safe_process_memory_reading(self.process_memory_sampler)
        )

        return ScannerRunResult(
            config=run_config,
            results=adjusted_results,
            scanned_symbols=sum(
                1 for result in adjusted_results if result.iteration_outcome != "not_run"
            ),
            failed_symbols=sum(1 for result in adjusted_results if _is_scan_error(result)),
            trade_ideas_created=sum(1 for result in adjusted_results if result.trade_idea is not None),
            dry_run_alerts_created=sum(
                1 for result in adjusted_results if ScannerPipelineStatus.ALERT_DRY_RUN_CREATED in result.status_history
            ),
            journal_entries_created=sum(1 for result in adjusted_results if result.journal_entry is not None),
            cache_stats=_cache_stats_for(client, run_config),
            retry_diagnostics=_retry_diagnostics_for(client),
            resume_metadata=dict(resume_metadata or {}),
            runtime_stats=_runtime_stats_for(
                adjusted_results,
                total_symbols=total_symbols,
                total_runtime_seconds=total_runtime_seconds,
                global_timeout_hit=global_timeout_hit,
                process_memory=_process_memory_stats_for(process_memory_readings),
            ),
            market_regime=market_regime,
            regime_adjustments=market_regime.adjustment,
            regime_warnings=market_regime.warnings,
            global_context=global_context,
        )

    def _exchange_client_for(self, config: ScannerRunConfig) -> tuple[BaseExchangeClient, bool]:
        cache = self.market_data_cache
        if self.exchange_client is not None:
            client = self.exchange_client
            owns_client = False
        elif config.exchange == "binance":
            client = BinanceFuturesClient(timeout=config.request_timeout_sec)
            owns_client = True
        else:
            client = BybitLinearClient(timeout=config.request_timeout_sec)
            owns_client = True

        if config.cache_enabled and not isinstance(client, CachedMarketDataClient):
            if cache is None:
                cache = MarketDataCache(
                    enabled=True,
                    ttl_seconds=config.cache_ttl_seconds,
                    file_path=config.cache_file,
                )
            client = CachedMarketDataClient(client, cache)
        return client, owns_client

    async def _build_global_context(
        self,
        client: BaseExchangeClient,
        config: ScannerRunConfig,
        *,
        progress: Callable[[str], Any] | None = None,
    ) -> GlobalContextSnapshot:
        if config.decision_timestamp is None:
            raise RuntimeError("scanner decision_timestamp must be resolved before global context")
        generated_at = config.decision_timestamp
        weekend_context = build_weekend_context(generated_at)
        btc_context = ContextValue.unavailable(
            source=f"internal_market_data:{config.exchange}",
            reason="BTC context disabled by configuration",
        )
        btc_d_context = ContextValue.unavailable(
            source="coinpaprika:/v1/global",
            reason="BTC.D context disabled by configuration",
        )
        await _emit_progress(progress, "Preparing global market context...")

        tasks: list[tuple[str, Any]] = []
        if config.btc_context_enabled:
            tasks.append(("btc", self._build_internal_btc_context(client, config)))
        if config.btc_d_context_enabled:
            tasks.append(("btc_d", self._build_btc_d_context(config)))
        if tasks:
            values = await asyncio.gather(
                *(task for _label, task in tasks),
                return_exceptions=True,
            )
            for (label, _task), value in zip(tasks, values, strict=True):
                if isinstance(value, BaseException):
                    context_value = ContextValue.error(
                        source=(
                            f"internal_market_data:{config.exchange}"
                            if label == "btc"
                            else "coinpaprika:/v1/global"
                        ),
                        reason=f"{label} context failed: {_clean_error_message(value)}",
                    )
                else:
                    context_value = value
                if label == "btc":
                    btc_context = context_value
                else:
                    btc_d_context = context_value

        snapshot = build_global_context_snapshot(
            generated_at=generated_at,
            btc_context=btc_context,
            btc_d_context=btc_d_context,
            weekend_context=weekend_context,
        )
        diagnostics = snapshot.diagnostics
        self.logger.info(
            "Global context status=%s btc=%s btc_d=%s weekend=%s btc_d_cache_hit=%s.",
            diagnostics.global_context_status.value,
            diagnostics.btc_context_status.value,
            diagnostics.btc_d_context_status.value,
            diagnostics.weekend_context_status.value,
            diagnostics.btc_d_cache_hit,
        )
        return snapshot

    async def _build_internal_btc_context(
        self,
        client: BaseExchangeClient,
        config: ScannerRunConfig,
    ) -> ContextValue:
        async def fetch_timeframe(
            timeframe: str,
        ) -> tuple[str, tuple[Any, ...], str | None]:
            limit_warnings: list[str] = []
            fetch_limit = _timeframe_fetch_limit(config, timeframe, limit_warnings)
            try:
                candles = await self._request_public_api(
                    config,
                    f"BTCUSDT {timeframe} candles for global context",
                    lambda: client.get_klines("BTCUSDT", timeframe, fetch_limit),
                    timeout_sec=_optional_request_timeout(config),
                )
                closed = self._closed_candles_for_analysis(
                    candles,
                    symbol="BTCUSDT",
                    timeframe=timeframe,
                    config=config,
                    minimum_closed_history=0,
                )
                if not closed:
                    return timeframe, (), f"BTC {timeframe} has no closed candles"
                return timeframe, tuple(closed), None
            except Exception as exc:
                self.logger.debug(
                    "BTC global context candles unavailable timeframe=%s: %s",
                    timeframe,
                    exc,
                )
                return timeframe, (), f"BTC {timeframe} candles unavailable: {_clean_error_message(exc)}"

        fetched = await asyncio.gather(
            *(fetch_timeframe(timeframe) for timeframe in BTC_CONTEXT_TIMEFRAMES),
            self._fetch_optional_market_data(client, "BTCUSDT", config),
        )
        optional_data = fetched[-1]
        timeframe_results = fetched[:-1]
        candles_by_timeframe = {
            timeframe: candles
            for timeframe, candles, _reason in timeframe_results
            if candles
        }
        unavailable_reasons = {
            timeframe: reason
            for timeframe, _candles, reason in timeframe_results
            if reason is not None
        }
        return build_internal_btc_context(
            candles_by_timeframe=candles_by_timeframe,
            generated_at=config.decision_timestamp,
            technical_agent=self.technical_agent,
            exchange=config.exchange,
            funding=optional_data.funding,
            open_interest=optional_data.open_interest,
            open_interest_history=optional_data.open_interest_history,
            unavailable_reasons=unavailable_reasons,
        )

    async def _build_btc_d_context(self, config: ScannerRunConfig) -> ContextValue:
        service = self.btc_d_context_service
        if service is None:
            provider = CoinPaprikaBtcDominanceProvider(
                timeout_seconds=config.btc_d_request_timeout_sec,
                log=self.logger,
            )
            service = BtcDominanceContextService(
                provider,
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
            self.btc_d_context_service = service
        return await service.get_context()

    def _microstructure_snapshot(
        self,
        symbol: str,
        config: ScannerRunConfig,
    ) -> MicrostructureFlowSnapshot | None:
        if not config.microstructure_flow_enabled:
            return None
        if config.exchange != "binance":
            return MicrostructureFlowSnapshot.unavailable(
                symbol=symbol,
                reason="unsupported_exchange",
            )
        service = self.microstructure_flow_service
        if service is None:
            return MicrostructureFlowSnapshot.unavailable(
                symbol=symbol,
                reason="service_not_running",
            )
        try:
            return service.snapshot(symbol)
        except Exception as exc:
            self.logger.warning(
                "Microstructure service failed safely for symbol=%s: %s",
                symbol,
                type(exc).__name__,
            )
            return MicrostructureFlowSnapshot.unavailable(
                symbol=symbol,
                reason=f"service_error:{type(exc).__name__}",
                status=ContextStatus.ERROR,
            )

    async def _scan_symbol(
        self,
        symbol_config: ScannerSymbolConfig,
        config: ScannerRunConfig,
        client: BaseExchangeClient,
        *,
        global_context: GlobalContextSnapshot | None = None,
        progress: Callable[[str], Any] | None = None,
    ) -> ScannerSymbolResult:
        symbol = symbol_config.symbol
        microstructure_flow = self._microstructure_snapshot(symbol, config)
        candles = await self._fetch_primary_candles(client, symbol, config, progress=progress)
        technical_candles = _technical_candles(candles)
        current_price = _current_price_from_candles(candles)
        await _emit_progress(progress, "Fetching derivatives...")
        optional_data = await self._fetch_optional_market_data(client, symbol, config)

        ticker_price = _decimal_field(optional_data.ticker, ("last_price", "mark_price"))
        if ticker_price != NA:
            current_price = ticker_price

        technical = self.technical_agent.analyze(technical_candles, timeframe=config.interval)
        base_missing = list(optional_data.missing_data)
        base_unverified = list(optional_data.unverified_data)
        if technical.analysis_status == TechnicalAnalysisStatus.INSUFFICIENT_DATA:
            base_missing.append(_technical_data_diagnostic(technical))
        elif technical.analysis_status == TechnicalAnalysisStatus.DATA_ERROR:
            base_unverified.append(_technical_data_diagnostic(technical))
        derivatives_enrichment = enrich_derivatives(
            _derivatives_enrichment_input(
                symbol=symbol,
                exchange=config.exchange,
                candles=candles,
                current_price=current_price,
                optional_data=optional_data,
                interval=config.interval,
            )
        )
        base_missing.extend(derivatives_enrichment.missing_data)
        base_unverified.extend(derivatives_enrichment.unverified_data)
        if technical.analysis_status == TechnicalAnalysisStatus.DATA_ERROR:
            strategy_execution = _StrategyExecution(
                strategy_name=config.strategy_name or NA,
                decision_timestamp=config.decision_timestamp,
            )
        else:
            strategy_execution = await self._run_strategy(
                client=client,
                symbol=symbol,
                config=config,
                primary_candles=candles,
                current_price=current_price,
                optional_data=optional_data,
                technical=technical,
                derivatives_enrichment=derivatives_enrichment,
                global_context=global_context,
                microstructure_flow=microstructure_flow,
                progress=progress,
            )
        strategy_execution = strategy_execution.model_copy(
            update={"microstructure_flow": microstructure_flow}
        )
        base_missing.extend(strategy_execution.strategy_missing_data)
        base_unverified.extend(strategy_execution.strategy_unverified_data)
        if microstructure_flow is not None and not microstructure_flow.verified:
            flow_missing, flow_unverified = _microstructure_data_health_diagnostics(
                microstructure_flow
            )
            base_missing.extend(flow_missing)
            base_unverified.extend(flow_unverified)
        await _emit_progress(progress, "Scoring...")

        if not technical.is_valid:
            reason = "; ".join(technical.errors) if technical.errors else "Technical structure is invalid."
            return self._symbol_result(
                symbol=symbol,
                status=ScannerPipelineStatus.REJECTED_BY_TECHNICAL,
                status_history=(ScannerPipelineStatus.REJECTED_BY_TECHNICAL,),
                candles=candles,
                current_price=current_price,
                optional_data=optional_data,
                missing_data=base_missing,
                unverified_data=base_unverified,
                rejection_reason=reason,
                rejection_stage_override=_technical_rejection_stage(technical),
                technical_result=technical,
                derivatives_enrichment=derivatives_enrichment,
                strategy_execution=strategy_execution,
            )

        derivatives_input = _derivatives_input(
            candles=candles,
            ticker=optional_data.ticker,
            funding=optional_data.funding,
            open_interest=optional_data.open_interest,
            previous_open_interest=optional_data.previous_open_interest,
            volume_z_score=technical.volume_z_score,
        )
        derivatives = self.derivatives_agent.analyze(derivatives_input)
        base_missing.extend(_missing_data_from_derivatives(derivatives))
        base_unverified.extend(_unverified_data_from_derivatives(derivatives))

        candidate: _CandidateSetup
        if config.enable_strategy_output and config.strategy_name == LIQUIDITY_GRAB_STRATEGY_NAME:
            if strategy_execution.selected_setup is None:
                return self._symbol_result(
                    symbol=symbol,
                    status=ScannerPipelineStatus.SCANNED_NO_SETUP,
                    status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
                    candles=candles,
                    current_price=current_price,
                    optional_data=optional_data,
                    missing_data=base_missing,
                    unverified_data=base_unverified,
                    rejection_reason=NO_VALID_STRATEGY_SETUP_REASON,
                    technical_result=technical,
                    derivatives_result=derivatives,
                    derivatives_enrichment=derivatives_enrichment,
                    strategy_execution=strategy_execution,
                    rejection_stage_override="strategy",
                )
            candidate = _candidate_from_strategy_setup(
                setup=strategy_execution.selected_setup,
                symbol=symbol,
                exchange=config.exchange,
                fallback_interval=config.interval,
            )
        else:
            candidate_result = _build_candidate(
                symbol=symbol,
                exchange=config.exchange,
                interval=config.interval,
                candles=candles,
                current_price=current_price,
                technical=technical,
            )
            if candidate_result.candidate is None:
                return self._symbol_result(
                    symbol=symbol,
                    status=candidate_result.status,
                    status_history=(candidate_result.status,),
                    candles=candles,
                    current_price=current_price,
                    optional_data=optional_data,
                    missing_data=[*base_missing, *candidate_result.missing_data],
                    unverified_data=base_unverified,
                    rejection_reason=candidate_result.reason,
                    technical_result=technical,
                    derivatives_result=derivatives,
                    derivatives_enrichment=derivatives_enrichment,
                    strategy_execution=strategy_execution,
                )
            candidate = candidate_result.candidate

        derivative_rejection = _derivatives_rejection(candidate.direction, derivatives_enrichment)
        if derivative_rejection is not None:
            return self._symbol_result(
                symbol=symbol,
                status=ScannerPipelineStatus.REJECTED_BY_DERIVATIVES,
                status_history=(ScannerPipelineStatus.REJECTED_BY_DERIVATIVES,),
                candles=candles,
                current_price=current_price,
                optional_data=optional_data,
                missing_data=base_missing,
                unverified_data=base_unverified,
                rejection_reason=derivative_rejection,
                technical_result=technical,
                derivatives_result=derivatives,
                derivatives_enrichment=derivatives_enrichment,
                strategy_execution=strategy_execution,
            )

        data_quality_score = _data_quality_score(technical, derivatives, base_missing)
        risk_decision = self.risk_manager.analyze(
            RiskManagerInput(
                account_equity=config.account_equity,
                risk_per_trade_pct=config.risk_per_trade_pct,
                entry_price=candidate.entry_price,
                stop_loss=candidate.stop_loss,
                take_profit_targets=candidate.take_profit_targets,
                direction=candidate.direction,
                leverage=config.leverage,
                max_daily_risk_pct=config.max_daily_risk_pct,
                current_daily_loss_pct=config.current_daily_loss_pct,
                data_quality_score=data_quality_score,
                invalidation_reason=candidate.invalidation,
            )
        )
        if not risk_decision.approved:
            return self._symbol_result(
                symbol=symbol,
                status=ScannerPipelineStatus.REJECTED_BY_RISK,
                status_history=(ScannerPipelineStatus.REJECTED_BY_RISK,),
                candles=candles,
                current_price=current_price,
                optional_data=optional_data,
                missing_data=base_missing,
                unverified_data=base_unverified,
                rejection_reason=_risk_rejection_reason(risk_decision),
                technical_result=technical,
                derivatives_result=derivatives,
                derivatives_enrichment=derivatives_enrichment,
                risk_decision=risk_decision,
                strategy_execution=strategy_execution,
            )

        target_integrity = _target_integrity_decision(strategy_execution, candidate)
        strategy_execution = (
            target_integrity.strategy_execution
            if target_integrity.strategy_execution is not None
            else _strategy_execution_with_target_integrity_pass(strategy_execution)
        )
        effective_technical_score = _technical_score_for_scoring(technical, strategy_execution)
        strategy_catalyst_score = _strategy_catalyst_score(strategy_execution.selected_setup)
        qualification_rr = _qualification_rr_for_scoring(strategy_execution, risk_decision)
        score_result = self.scoring_engine.score(
            {
                "technical_score": Decimal(effective_technical_score),
                "derivatives_score": _scoring_derivatives_score(derivatives_enrichment),
                "risk_approved": risk_decision.approved,
                "best_rr": qualification_rr,
                "liquidity_score": _liquidity_score(candles, optional_data.ticker),
                "catalyst_score": strategy_catalyst_score,
                "data_quality_score": data_quality_score,
                "invalidation_present": risk_decision.invalidation_reason != NA,
                "setup_location": candidate.setup_location,
                "risk_rejection_reasons": tuple(violation.message for violation in risk_decision.violations),
                "missing_data": _scoring_missing_data(base_missing),
                "unverified_data": _scoring_unverified_data(base_unverified),
            }
        )
        if target_integrity.blocked:
            return self._symbol_result(
                symbol=symbol,
                status=ScannerPipelineStatus.SCANNED_NO_SETUP,
                status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
                candles=candles,
                current_price=current_price,
                optional_data=optional_data,
                missing_data=base_missing,
                unverified_data=base_unverified,
                rejection_reason=target_integrity.reason,
                technical_result=technical,
                derivatives_result=derivatives,
                derivatives_enrichment=derivatives_enrichment,
                risk_decision=risk_decision,
                score_result=score_result,
                strategy_execution=strategy_execution,
                rejection_stage_override=TARGET_INTEGRITY_FAILED_GATE,
                technical_score_override=effective_technical_score,
            )
        if (
            not score_result.hard_filter_result.passed
            or score_result.decision == "reject"
            or score_result.total_score < config.min_score_for_idea
        ):
            return self._symbol_result(
                symbol=symbol,
                status=ScannerPipelineStatus.REJECTED_BY_SCORING,
                status_history=(ScannerPipelineStatus.REJECTED_BY_SCORING,),
                candles=candles,
                current_price=current_price,
                optional_data=optional_data,
                missing_data=base_missing,
                unverified_data=base_unverified,
                rejection_reason=_scoring_rejection_reason(score_result, config.min_score_for_idea),
                technical_result=technical,
                derivatives_result=derivatives,
                derivatives_enrichment=derivatives_enrichment,
                risk_decision=risk_decision,
                score_result=score_result,
                strategy_execution=strategy_execution,
                rejection_stage_override=_scoring_rejection_stage(score_result),
                technical_score_override=effective_technical_score,
            )

        trade_idea = self.trade_idea_agent.create(
            {
                "symbol": symbol,
                "exchange": config.exchange,
                "market_type": "perpetual",
                "direction": candidate.direction,
                "timeframe": config.interval,
                "setup_type": candidate.setup_type,
                "entry_low": candidate.entry_low,
                "entry_high": candidate.entry_high,
                "entry_reference": candidate.entry_price,
                "stop_loss": candidate.stop_loss,
                "take_profit_targets": candidate.take_profit_targets,
                "invalidation": candidate.invalidation,
                "opportunity_score": score_result.total_score,
                "opportunity_grade": score_result.grade,
                "opportunity_decision": score_result.decision,
                "risk_approved": risk_decision.approved,
                "best_rr": qualification_rr,
                "technical_summary": candidate.technical_summary,
                "derivatives_summary": _derivatives_enrichment_summary(derivatives_enrichment),
                "confirmed_facts": candidate.confirmed_facts,
                "missing_data": _unique_strings(base_missing),
                "unverified_data": _unique_strings(base_unverified),
                "cancel_condition": candidate.cancel_condition,
                "leverage": config.leverage,
            }
        )
        if not trade_idea.quality_gate_result.passed:
            return self._symbol_result(
                symbol=symbol,
                status=ScannerPipelineStatus.REJECTED_BY_SCORING,
                status_history=(ScannerPipelineStatus.REJECTED_BY_SCORING,),
                candles=candles,
                current_price=current_price,
                optional_data=optional_data,
                missing_data=base_missing,
                unverified_data=base_unverified,
                rejection_reason=_trade_idea_rejection_reason(trade_idea),
                technical_result=technical,
                derivatives_result=derivatives,
                derivatives_enrichment=derivatives_enrichment,
                risk_decision=risk_decision,
                score_result=score_result,
                strategy_execution=strategy_execution,
                technical_score_override=effective_technical_score,
            )

        status_history = [ScannerPipelineStatus.IDEA_CREATED]
        alert_result = await self.alert_agent.send(
            {
                "trade_idea": trade_idea,
                "dry_run": config.dry_run_alerts,
                "deduplication_key": f"{symbol}-{config.interval}-{candidate.setup_type}",
            }
        )
        if alert_result.dry_run:
            status_history.append(ScannerPipelineStatus.ALERT_DRY_RUN_CREATED)

        journal_entry = self.journal_agent.create(
            {
                "symbol": trade_idea.symbol,
                "exchange": trade_idea.exchange,
                "direction": trade_idea.direction,
                "timeframe": trade_idea.timeframe,
                "setup_type": trade_idea.setup_type,
                "status": JournalStatus.WATCHING,
                "entry_low": trade_idea.entry_zone.low,
                "entry_high": trade_idea.entry_zone.high,
                "stop_loss": trade_idea.stop_loss.price,
                "take_profit_targets": tuple(target.price for target in trade_idea.take_profits),
                "invalidation": trade_idea.invalidation,
                "best_rr": trade_idea.best_rr,
                "confidence_score": trade_idea.confidence_score,
                "grade": trade_idea.grade,
                "reason_for_trade": trade_idea.reason_for_trade,
                "confirmed_facts": trade_idea.confirmed_facts,
                "missing_data": trade_idea.missing_data,
                "unverified_data": trade_idea.unverified_data,
                "risk_warning": trade_idea.risk_warning,
                "notes": "Created by Phase 10 scanner runner in dry-run alert flow.",
            }
        )
        status_history.append(ScannerPipelineStatus.JOURNAL_ENTRY_CREATED)

        return self._symbol_result(
            symbol=symbol,
            status=ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
            status_history=tuple(status_history),
            candles=candles,
            current_price=current_price,
            optional_data=optional_data,
            missing_data=base_missing,
            unverified_data=base_unverified,
            technical_result=technical,
            derivatives_result=derivatives,
            derivatives_enrichment=derivatives_enrichment,
            risk_decision=risk_decision,
            score_result=score_result,
            trade_idea=trade_idea,
            alert_result=alert_result,
            journal_entry=journal_entry,
            strategy_execution=strategy_execution,
            technical_score_override=effective_technical_score,
        )

    def _closed_candles_for_analysis(
        self,
        candles: Sequence[Any],
        *,
        symbol: str,
        timeframe: str,
        config: ScannerRunConfig,
        minimum_closed_history: int,
    ) -> tuple[Any, ...]:
        if config.decision_timestamp is None:
            raise RuntimeError("scanner decision_timestamp must be resolved before candle analysis")
        window = closed_candles_as_of(
            candles,
            timeframe=timeframe,
            decision_timestamp=config.decision_timestamp,
            minimum_closed_history=minimum_closed_history,
        )
        if window.excluded_unclosed_count:
            self.logger.debug(
                "Excluded %s unclosed/future candles for symbol=%s timeframe=%s as_of=%s.",
                window.excluded_unclosed_count,
                symbol,
                timeframe,
                window.decision_timestamp.isoformat(),
            )
        return window.candles

    async def _fetch_primary_candles(
        self,
        client: BaseExchangeClient,
        symbol: str,
        config: ScannerRunConfig,
        *,
        progress: Callable[[str], Any] | None = None,
    ) -> Sequence[Any]:
        primary_timeframe = config.interval.strip().lower()
        limit_warnings: list[str] = []
        await _emit_progress(progress, _progress_message_for_timeframe(config, primary_timeframe))
        if config.exchange == "binance" and primary_timeframe == "2d":
            source_limit = _synthetic_2d_source_limit(config, limit_warnings)
            source_candles = await self._request_public_api(
                config,
                f"{symbol} {SYNTHETIC_2D_SOURCE_TIMEFRAME} candles for synthetic 2d",
                lambda: client.get_klines(symbol, SYNTHETIC_2D_SOURCE_TIMEFRAME, source_limit),
            )
            for warning in _unique_strings(limit_warnings):
                self.logger.warning("Scanner candle limit adjusted for symbol=%s: %s", symbol, warning)
            synthetic = resample_ohlcv_candles(
                source_candles,
                target_interval="2d",
                decision_timestamp=config.decision_timestamp,
            )
            return self._closed_candles_for_analysis(
                synthetic,
                symbol=symbol,
                timeframe="2d",
                config=config,
                minimum_closed_history=0,
            )
        fetch_limit = _timeframe_fetch_limit(config, primary_timeframe, limit_warnings)
        for warning in _unique_strings(limit_warnings):
            self.logger.warning("Scanner candle limit adjusted for symbol=%s: %s", symbol, warning)
        candles = await self._request_public_api(
            config,
            f"{symbol} {config.interval} candles",
            lambda: client.get_klines(symbol, config.interval, fetch_limit),
        )
        return self._closed_candles_for_analysis(
            candles,
            symbol=symbol,
            timeframe=primary_timeframe,
            config=config,
            minimum_closed_history=0,
        )

    async def _fetch_market_regime_context(
        self,
        client: BaseExchangeClient,
        config: ScannerRunConfig,
        *,
        progress: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        await _emit_progress(progress, "Evaluating market climate...")
        timeframe = config.bias_timeframe.strip().lower()
        if timeframe == "2d":
            timeframe = SYNTHETIC_2D_SOURCE_TIMEFRAME
        limit_warnings: list[str] = []
        fetch_limit = _timeframe_fetch_limit(config, timeframe, limit_warnings)
        context: dict[str, Any] = {
            "btc_candles": (),
            "eth_candles": (),
            "candle_timeframe": timeframe,
            "decision_timestamp": config.decision_timestamp,
            "missing_data": [],
        }
        for symbol, key in (("BTCUSDT", "btc_candles"), ("ETHUSDT", "eth_candles")):
            try:
                candles = await self._request_public_api(
                    config,
                    f"{symbol} {timeframe} candles for market climate",
                    lambda symbol=symbol: client.get_klines(symbol, timeframe, fetch_limit),
                    timeout_sec=_optional_request_timeout(config),
                )
                context[key] = self._closed_candles_for_analysis(
                    candles,
                    symbol=symbol,
                    timeframe=timeframe,
                    config=config,
                    minimum_closed_history=MIN_REGIME_CLOSED_CANDLES,
                )
            except Exception as exc:
                self.logger.debug("Market climate %s candles unavailable: %s", symbol, exc)
                context["missing_data"].append(f"{symbol}_candles: N/A ({exc})")
        context["missing_data"].extend(limit_warnings)
        return context

    async def _run_strategy(
        self,
        *,
        client: BaseExchangeClient,
        symbol: str,
        config: ScannerRunConfig,
        primary_candles: Sequence[Any],
        current_price: MaybeDecimal,
        optional_data: _OptionalMarketData,
        technical: TechnicalStructureResult,
        derivatives_enrichment: DerivativesEnrichmentResult | None = None,
        global_context: GlobalContextSnapshot | None = None,
        microstructure_flow: MicrostructureFlowSnapshot | None = None,
        progress: Callable[[str], Any] | None = None,
    ) -> _StrategyExecution:
        if not config.enable_strategy_output or config.strategy_name is None:
            execution_timeframe = config.execution_timeframe.strip().lower()
            primary_timeframe = config.interval.strip().lower()
            profile_candles = primary_candles if primary_timeframe == execution_timeframe else ()
            volume_profile = _volume_profile_for_timeframe(
                symbol=symbol,
                timeframe=execution_timeframe,
                candles=profile_candles,
            )
            return _StrategyExecution(
                strategy_missing_data=volume_profile.missing_data,
                volume_profile=volume_profile,
                execution_candles=tuple(profile_candles),
                execution_timeframe=execution_timeframe,
                decision_timestamp=config.decision_timestamp,
            )

        candles_by_timeframe, timeframe_missing, timeframe_context = await self._fetch_strategy_timeframe_candles(
            client=client,
            symbol=symbol,
            config=config,
            primary_candles=primary_candles,
            progress=progress,
        )
        execution_timeframe = config.execution_timeframe.strip().lower()
        execution_volume_profile = _volume_profile_for_timeframe(
            symbol=symbol,
            timeframe=execution_timeframe,
            candles=candles_by_timeframe.get(execution_timeframe, ()),
        )
        higher_timeframe_volume_profile = _optional_12h_volume_profile(
            symbol=symbol,
            candles=candles_by_timeframe.get("12h", ()),
        )
        base_input = _liquidity_grab_input(
            symbol=symbol,
            candles_by_timeframe=candles_by_timeframe,
            current_price=current_price,
            optional_data=optional_data,
            technical=technical,
            aggressive_toggle=config.aggressive_toggle,
            min_rr=config.min_rr,
            htf_timeframe=config.htf_timeframe,
            structure_timeframe=config.structure_timeframe,
            bias_timeframe=config.bias_timeframe,
            execution_timeframe=config.execution_timeframe,
            confirmation_timeframe=config.confirmation_timeframe,
            timeframe_context=timeframe_context,
            volume_profile=execution_volume_profile,
            derivatives_enrichment=derivatives_enrichment,
            global_context=global_context,
            microstructure_flow=microstructure_flow,
        )

        strategy_results: dict[str, LiquidityGrabResult] = {}
        diagnostics: dict[str, Any] = {}
        valid_modes: list[str] = []
        rejected_modes: list[str] = []
        missing_data = list(timeframe_missing)
        missing_data.extend(execution_volume_profile.missing_data)
        unverified_data: list[str] = []
        timeframe_limit_warnings = _sequence_from_diagnostics(timeframe_context.get("timeframe_limit_warnings"))
        formatted_output = NA
        selected_setup: LiquidityGrabSetup | None = None

        for mode in config.strategy_modes:
            mode_name = mode.value
            strategy_input = LiquidityGrabInput.model_validate({**base_input, "mode": mode})
            try:
                result = self.strategy_engine.analyze(strategy_input)
            except Exception as exc:
                self.logger.warning("Strategy %s failed for symbol=%s mode=%s: %s", config.strategy_name, symbol, mode_name, exc)
                diagnostics[mode_name] = {"error": str(exc)}
                rejected_modes.append(mode_name)
                continue

            strategy_results[mode_name] = result
            setup = _strategy_setup_for_mode(result, mode)
            mode_diagnostics = _strategy_diagnostics_for_setup(setup)
            mode_diagnostics["required_rr"] = setup.effective_minimum_rr
            pullback_intelligence = build_pullback_intelligence(mode_diagnostics)
            mode_diagnostics["pullback_intelligence"] = pullback_intelligence.model_dump(mode="json")
            target_intelligence = _target_intelligence_for_setup(
                setup=setup,
                diagnostics=mode_diagnostics,
                candles_by_timeframe=candles_by_timeframe,
                technical=technical,
                volume_profile=execution_volume_profile,
                higher_timeframe_volume_profile=higher_timeframe_volume_profile,
            )
            mode_diagnostics["target_intelligence"] = target_intelligence.model_dump(mode="json")
            if timeframe_limit_warnings:
                mode_diagnostics["timeframe_limit_warnings"] = timeframe_limit_warnings
            diagnostics[mode_name] = mode_diagnostics
            missing_data.extend(result.missing_data)
            unverified_data.extend(result.unverified_data)

            if formatted_output == NA and config.include_formatted_strategy_output:
                formatted_output = result.formatted_output.full_text

            if _is_valid_strategy_setup(setup):
                valid_modes.append(mode_name)
                if selected_setup is None:
                    selected_setup = setup
            else:
                rejected_modes.append(mode_name)

        return _StrategyExecution(
            strategy_name=config.strategy_name,
            strategy_results=strategy_results,
            formatted_strategy_output=formatted_output,
            strategy_diagnostics=diagnostics,
            valid_strategy_modes=_unique_strings(valid_modes),
            rejected_strategy_modes=_unique_strings(rejected_modes),
            strategy_missing_data=_unique_strings(missing_data),
            strategy_unverified_data=_unique_strings(unverified_data),
            volume_profile=execution_volume_profile,
            volume_profile_12h=higher_timeframe_volume_profile,
            selected_setup=selected_setup,
            execution_candles=tuple(candles_by_timeframe.get(execution_timeframe, ())),
            execution_timeframe=execution_timeframe,
            decision_timestamp=config.decision_timestamp,
            pullback_intelligence=_representative_pullback_intelligence(
                diagnostics,
                valid_modes=_unique_strings(valid_modes),
                rejected_modes=_unique_strings(rejected_modes),
            ),
            target_intelligence=_representative_target_intelligence(
                diagnostics,
                valid_modes=_unique_strings(valid_modes),
                rejected_modes=_unique_strings(rejected_modes),
            ),
        )

    async def _fetch_strategy_timeframe_candles(
        self,
        *,
        client: BaseExchangeClient,
        symbol: str,
        config: ScannerRunConfig,
        primary_candles: Sequence[Any],
        progress: Callable[[str], Any] | None = None,
    ) -> tuple[dict[str, Sequence[Any]], tuple[str, ...], dict[str, Any]]:
        candles_by_timeframe: dict[str, Sequence[Any]] = {}
        missing_data: list[str] = []
        limit_warnings: list[str] = []
        primary_timeframe = config.interval.strip().lower()
        htf_source: str = NA

        if config.htf_timeframe.strip().lower() == "2d":
            source_candles: Sequence[Any] = ()
            synthetic_data_error = False
            await _emit_progress(progress, "Fetching HTF 2d...")
            try:
                if primary_timeframe == SYNTHETIC_2D_SOURCE_TIMEFRAME:
                    source_candles = primary_candles
                else:
                    source_limit = _synthetic_2d_source_limit(config, limit_warnings)
                    source_candles = await self._request_public_api(
                        config,
                        f"{symbol} {SYNTHETIC_2D_SOURCE_TIMEFRAME} candles for synthetic 2d",
                        lambda: client.get_klines(symbol, SYNTHETIC_2D_SOURCE_TIMEFRAME, source_limit),
                    )
                synthetic_2d = resample_ohlcv_candles(
                    source_candles,
                    target_interval="2d",
                    decision_timestamp=config.decision_timestamp,
                )
            except CandleIntegrityError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "Synthetic 2D candle creation failed for symbol=%s from 1d source: %s",
                    symbol,
                    exc,
                )
                synthetic_2d = []
                synthetic_data_error = True

            if synthetic_2d:
                closed_synthetic_2d = self._closed_candles_for_analysis(
                    synthetic_2d,
                    symbol=symbol,
                    timeframe="2d",
                    config=config,
                    minimum_closed_history=0,
                )
                if len(closed_synthetic_2d) >= MIN_STRATEGY_CLOSED_CANDLES:
                    candles_by_timeframe["2d"] = closed_synthetic_2d
                    htf_source = "synthetic_from_1d"
                else:
                    missing_data.extend(_strategy_history_diagnostics("2d", len(closed_synthetic_2d)))
            else:
                if synthetic_data_error or not source_candles:
                    missing_data.extend(_strategy_data_error_diagnostics("2d"))
                else:
                    missing_data.extend(_strategy_history_diagnostics("2d", 0))

        for timeframe in _direct_strategy_timeframes(config):
            if timeframe == primary_timeframe:
                if len(primary_candles) >= MIN_STRATEGY_CLOSED_CANDLES:
                    candles_by_timeframe[timeframe] = primary_candles
                else:
                    missing_data.extend(_strategy_history_diagnostics(timeframe, len(primary_candles)))
                continue

            if config.fast_mode and _fast_mode_skips_timeframe(config, timeframe):
                missing_data.append(f"candles_{timeframe}: N/A")
                limit_warnings.append(f"{timeframe} candles skipped in fast mode.")
                continue

            await _emit_progress(progress, _progress_message_for_timeframe(config, timeframe))
            try:
                fetch_limit = _timeframe_fetch_limit(config, timeframe, limit_warnings)
                candles = await self._request_public_api(
                    config,
                    f"{symbol} {timeframe} candles",
                    lambda: client.get_klines(symbol, timeframe, fetch_limit),
                )
            except Exception as exc:
                self.logger.warning("Optional strategy candles fetch failed for symbol=%s timeframe=%s: %s", symbol, timeframe, exc)
                missing_data.extend(_strategy_data_error_diagnostics(timeframe))
                continue

            if not candles:
                missing_data.extend(_strategy_data_error_diagnostics(timeframe))
                continue
            try:
                closed_candles = self._closed_candles_for_analysis(
                    candles,
                    symbol=symbol,
                    timeframe=timeframe,
                    config=config,
                    minimum_closed_history=0,
                )
            except CandleIntegrityError:
                raise
            if len(closed_candles) >= MIN_STRATEGY_CLOSED_CANDLES:
                candles_by_timeframe[timeframe] = closed_candles
            else:
                missing_data.extend(_strategy_history_diagnostics(timeframe, len(closed_candles)))

        return (
            candles_by_timeframe,
            _unique_strings(missing_data),
            {
                "htf_2d_context_source": htf_source,
                "timeframe_limit_warnings": _unique_strings(limit_warnings),
            },
        )

    async def _fetch_optional_market_data(
        self,
        client: BaseExchangeClient,
        symbol: str,
        config: ScannerRunConfig,
    ) -> _OptionalMarketData:
        missing_data: list[str] = []
        unverified_data: list[str] = []
        warnings: list[str] = []
        optional_specs = (
            ("ticker", "get_ticker"),
            ("funding_rate", "get_funding_rate"),
            ("funding_history", "get_funding_rate_history"),
            ("open_interest", "get_open_interest"),
            ("open_interest_history", "get_open_interest_history"),
            ("long_short_ratio", "get_long_short_ratio"),
        )
        skipped_fast_labels = {
            "funding_history",
            "open_interest_history",
            "long_short_ratio",
        } if config.fast_mode else set()
        optional_results = await asyncio.gather(
            *(
                self._optional_call(client, method_name, symbol, config, label)
                for label, method_name in optional_specs
                if label not in skipped_fast_labels
            )
        )
        values_by_label = {label: value for label, value, _missing, _warning in optional_results}
        for label, _method_name in optional_specs:
            if label in skipped_fast_labels:
                missing_data.append(f"{label}: N/A")
                warnings.append(f"{label} skipped in fast mode.")
                continue
            result = next((item for item in optional_results if item[0] == label), None)
            if result is None:
                continue
            _label, _value, missing, warning = result
            if missing is not None:
                missing_data.append(missing)
            if warning is not None:
                warnings.append(warning)

        ticker = values_by_label.get("ticker")
        funding = values_by_label.get("funding_rate")
        funding_history = values_by_label.get("funding_history")
        open_interest = values_by_label.get("open_interest")
        open_interest_history = values_by_label.get("open_interest_history")
        long_short_ratio = values_by_label.get("long_short_ratio")
        previous_open_interest = _previous_open_interest_from(open_interest)

        if previous_open_interest == NA:
            previous_open_interest = _previous_open_interest_from_history(open_interest_history)

        if previous_open_interest == NA:
            missing_data.append("previous_open_interest: N/A")

        return _OptionalMarketData(
            ticker=ticker,
            funding=funding,
            funding_history=funding_history if _is_sequence_data(funding_history) else None,
            open_interest=open_interest,
            open_interest_history=open_interest_history if _is_sequence_data(open_interest_history) else None,
            previous_open_interest=previous_open_interest,
            long_short_ratio=_normalize_optional_decimal(long_short_ratio),
            warnings=_unique_strings(warnings),
            missing_data=_unique_strings(missing_data),
            unverified_data=_unique_strings(unverified_data),
        )

    async def _optional_call(
        self,
        client: BaseExchangeClient,
        method_name: str,
        symbol: str,
        config: ScannerRunConfig,
        label: str,
    ) -> tuple[str, Any | None, str | None, str | None]:
        method = getattr(client, method_name, None)
        if not callable(method):
            return label, None, f"{label}: N/A", None
        try:
            value = await self._request_public_api(
                config,
                f"{symbol} {label}",
                lambda: method(symbol),
                timeout_sec=_optional_request_timeout(config),
            )
            return label, value, None, None
        except Exception as exc:
            self.logger.debug("Optional %s fetch failed for symbol=%s: %s", label, symbol, exc)
            return label, None, f"{label}: N/A", f"{label} unavailable from public endpoint: {exc}"

    async def _request_public_api(
        self,
        config: ScannerRunConfig,
        label: str,
        call: Callable[[], Any],
        *,
        timeout_sec: float | None = None,
    ) -> Any:
        timeout = timeout_sec if timeout_sec is not None else config.request_timeout_sec
        try:
            return await asyncio.wait_for(_maybe_await(call()), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ExchangeTimeoutError(
                f"{label} request timed out after {_format_seconds(timeout)} seconds"
            ) from exc

    def _symbol_result(
        self,
        *,
        symbol: str,
        status: ScannerPipelineStatus,
        status_history: tuple[ScannerPipelineStatus, ...],
        candles: Sequence[Any],
        current_price: MaybeDecimal,
        optional_data: _OptionalMarketData,
        missing_data: Sequence[str],
        unverified_data: Sequence[str],
        rejection_reason: str | None = None,
        technical_result: TechnicalStructureResult | None = None,
        derivatives_result: DerivativesOrderflowResult | None = None,
        derivatives_enrichment: DerivativesEnrichmentResult | None = None,
        risk_decision: RiskDecision | None = None,
        score_result: OpportunityScoreResult | None = None,
        trade_idea: TradeIdeaResult | None = None,
        alert_result: AlertResult | None = None,
        journal_entry: JournalEntryResult | None = None,
        strategy_execution: _StrategyExecution | None = None,
        rejection_stage_override: str | None = None,
        technical_score_override: MaybeInt | None = None,
    ) -> ScannerSymbolResult:
        cleaned_missing = _unique_strings(missing_data)
        cleaned_unverified = _unique_strings(unverified_data)
        strategy_execution = strategy_execution or _StrategyExecution()
        near_miss_intelligence = _near_miss_intelligence_for_result(
            status=status,
            rejection_reason=rejection_reason,
            strategy_execution=strategy_execution,
            trade_idea=trade_idea,
            alert_result=alert_result,
            journal_entry=journal_entry,
        )
        setup_quality = _setup_quality_for_result(
            symbol=symbol,
            status=status,
            rejection_reason=rejection_reason,
            strategy_execution=strategy_execution,
            technical_result=technical_result,
            derivatives_enrichment=derivatives_enrichment,
            risk_decision=risk_decision,
            score_result=score_result,
            trade_idea=trade_idea,
            alert_result=alert_result,
            journal_entry=journal_entry,
            missing_data=cleaned_missing,
            unverified_data=cleaned_unverified,
        )
        candidate_quality_grade = _display_decimal_or_text(
            getattr(setup_quality.quality_grade, "value", setup_quality.quality_grade)
        )
        return ScannerSymbolResult(
            symbol=symbol,
            status=status,
            status_history=status_history,
            rejection_reason=rejection_reason,
            candle_count=len(candles),
            current_price=current_price,
            funding_rate=derivatives_enrichment.funding_rate
            if derivatives_enrichment is not None
            else _decimal_field(optional_data.funding, ("funding_rate", "current_funding_rate")),
            open_interest=derivatives_enrichment.open_interest
            if derivatives_enrichment is not None
            else _decimal_field(optional_data.open_interest, ("open_interest", "current_open_interest", "oi")),
            candles_fetched=len(candles),
            latest_close=_current_price_from_candles(candles),
            latest_high=_decimal_field(candles[-1], ("high",)) if candles else NA,
            latest_low=_decimal_field(candles[-1], ("low",)) if candles else NA,
            technical_score=technical_score_override
            if technical_score_override is not None
            else technical_result.structure_score
            if technical_result is not None
            else NA,
            technical_status=(
                technical_result.analysis_status.value if technical_result is not None else NA
            ),
            technical_timeframe=technical_result.timeframe if technical_result is not None else NA,
            technical_required_bars=(
                technical_result.required_candles if technical_result is not None else NA
            ),
            technical_available_bars=(
                technical_result.available_candles if technical_result is not None else NA
            ),
            derivatives_score=derivatives_enrichment.derivatives_score
            if derivatives_enrichment is not None
            else derivatives_result.derivatives_score
            if derivatives_result is not None
            else NA,
            trend_context=technical_result.trend_context if technical_result is not None else NA,
            recent_range_high=technical_result.recent_range_high if technical_result is not None else NA,
            recent_range_low=technical_result.recent_range_low if technical_result is not None else NA,
            nearest_support=technical_result.nearest_support if technical_result is not None else NA,
            nearest_resistance=technical_result.nearest_resistance if technical_result is not None else NA,
            latest_swing_high=_latest_swing_price(technical_result.swing_highs if technical_result is not None else ()),
            latest_swing_low=_latest_swing_price(technical_result.swing_lows if technical_result is not None else ()),
            sweep_detected=technical_result.sweep.is_present if technical_result is not None else False,
            bos_detected=technical_result.bos.is_present if technical_result is not None else False,
            choch_detected=technical_result.choch.is_present if technical_result is not None else False,
            funding_direction=derivatives_result.funding.direction if derivatives_result is not None else NA,
            funding_severity=derivatives_result.funding.severity if derivatives_result is not None else NA,
            funding_status=derivatives_enrichment.funding_status if derivatives_enrichment is not None else NA,
            funding_extreme=derivatives_enrichment.funding_extreme if derivatives_enrichment is not None else NA,
            oi_direction=derivatives_enrichment.oi_direction
            if derivatives_enrichment is not None
            else derivatives_result.open_interest.direction
            if derivatives_result is not None
            else NA,
            open_interest_change_pct=derivatives_enrichment.open_interest_change_pct
            if derivatives_enrichment is not None
            else derivatives_result.open_interest.oi_change_percentage
            if derivatives_result is not None
            else NA,
            price_oi_relationship=derivatives_enrichment.price_oi_relationship
            if derivatives_enrichment is not None
            else derivatives_result.price_oi_relationship.classification
            if derivatives_result is not None
            else NA,
            price_direction=derivatives_enrichment.price_direction if derivatives_enrichment is not None else NA,
            long_short_ratio=derivatives_enrichment.long_short_ratio if derivatives_enrichment is not None else NA,
            crowding_risk=derivatives_enrichment.crowding_risk if derivatives_enrichment is not None else NA,
            squeeze_risk=derivatives_enrichment.squeeze_risk if derivatives_enrichment is not None else NA,
            derivatives_missing_data=derivatives_enrichment.missing_data if derivatives_enrichment is not None else (),
            derivatives_unverified_data=derivatives_enrichment.unverified_data
            if derivatives_enrichment is not None
            else (),
            derivatives_warnings=derivatives_enrichment.warnings if derivatives_enrichment is not None else (),
            rejection_stage=rejection_stage_override or _rejection_stage_for(status),
            rejection_reasons=_rejection_reasons_for(status, rejection_reason),
            missing_data=cleaned_missing,
            unverified_data=cleaned_unverified,
            lifecycle_execution_candles=tuple(strategy_execution.execution_candles),
            lifecycle_execution_timeframe=strategy_execution.execution_timeframe,
            lifecycle_decision_timestamp=strategy_execution.decision_timestamp,
            strategy_name=strategy_execution.strategy_name,
            strategy_results=strategy_execution.strategy_results,
            formatted_strategy_output=strategy_execution.formatted_strategy_output,
            strategy_diagnostics=strategy_execution.strategy_diagnostics,
            valid_strategy_modes=strategy_execution.valid_strategy_modes,
            rejected_strategy_modes=strategy_execution.rejected_strategy_modes,
            strategy_missing_data=strategy_execution.strategy_missing_data,
            strategy_unverified_data=strategy_execution.strategy_unverified_data,
            volume_profile=strategy_execution.volume_profile,
            volume_profile_12h=strategy_execution.volume_profile_12h,
            volume_profile_source=strategy_execution.volume_profile.source
            if strategy_execution.volume_profile is not None
            else NA,
            poc=strategy_execution.volume_profile.poc if strategy_execution.volume_profile is not None else NA,
            value_area_high=strategy_execution.volume_profile.value_area_high
            if strategy_execution.volume_profile is not None
            else NA,
            value_area_low=strategy_execution.volume_profile.value_area_low
            if strategy_execution.volume_profile is not None
            else NA,
            nearest_high_volume_node=strategy_execution.volume_profile.nearest_high_volume_node
            if strategy_execution.volume_profile is not None
            else NA,
            nearest_low_volume_node=strategy_execution.volume_profile.nearest_low_volume_node
            if strategy_execution.volume_profile is not None
            else NA,
            volume_profile_warnings=strategy_execution.volume_profile.warnings
            if strategy_execution.volume_profile is not None
            else (),
            technical_result=technical_result,
            derivatives_result=derivatives_result,
            derivatives_enrichment=derivatives_enrichment,
            microstructure_flow=strategy_execution.microstructure_flow,
            risk_decision=risk_decision,
            score_result=score_result,
            trade_idea=trade_idea,
            alert_result=alert_result,
            journal_entry=journal_entry,
            near_miss_intelligence=near_miss_intelligence,
            pullback_intelligence=strategy_execution.pullback_intelligence,
            target_intelligence=strategy_execution.target_intelligence,
            setup_quality=setup_quality,
            candidate_quality_grade=candidate_quality_grade,
            final_quality_grade=candidate_quality_grade,
        )


def _setup_quality_for_result(
    *,
    symbol: str,
    status: ScannerPipelineStatus,
    rejection_reason: str | None,
    strategy_execution: _StrategyExecution,
    technical_result: TechnicalStructureResult | None,
    derivatives_enrichment: DerivativesEnrichmentResult | None,
    risk_decision: RiskDecision | None,
    score_result: OpportunityScoreResult | None,
    trade_idea: TradeIdeaResult | None,
    alert_result: AlertResult | None,
    journal_entry: JournalEntryResult | None,
    missing_data: Sequence[str],
    unverified_data: Sequence[str],
) -> SetupQualityResult:
    diagnostics = _representative_strategy_diagnostics(strategy_execution)
    setup = strategy_execution.selected_setup
    mode = setup.mode.value if setup is not None else _strategy_mode_from_execution(strategy_execution)
    failed_gate = _strategy_failed_gate(diagnostics) if diagnostics else NA
    if status == ScannerPipelineStatus.REJECTED_BY_TECHNICAL and technical_result is not None:
        failed_gate = _technical_rejection_stage(technical_result)
    elif (
        status == ScannerPipelineStatus.REJECTED_BY_SCORING
        and _score_has_violation(score_result, "weak_technical_score")
    ):
        failed_gate = "technical_invalid"
    elif failed_gate == NA:
        if status == ScannerPipelineStatus.REJECTED_BY_DERIVATIVES:
            failed_gate = "derivatives_conflict"
        elif status == ScannerPipelineStatus.REJECTED_BY_RISK:
            failed_gate = "risk"
        elif status == ScannerPipelineStatus.REJECTED_BY_SCORING:
            failed_gate = "quality_filter"
        elif status == ScannerPipelineStatus.SCAN_ERROR:
            failed_gate = "scanner_error"

    gates_passed = _sequence_from_diagnostics(diagnostics.get("gates_passed")) if diagnostics else ()
    gates_failed = _sequence_from_diagnostics(diagnostics.get("gates_failed")) if diagnostics else ()
    bias = _first_non_na(
        getattr(setup, "bias", NA) if setup is not None else NA,
        diagnostics.get("bias") if diagnostics else NA,
        getattr(trade_idea, "direction", NA) if trade_idea is not None else NA,
    )
    rr_to_tp2 = _first_decimal(
        getattr(setup, "rr_to_tp2", NA) if setup is not None else NA,
        diagnostics.get("rr_to_tp2") if diagnostics else NA,
        getattr(risk_decision, "best_risk_reward_ratio", NA) if risk_decision is not None else NA,
    )
    best_rr = _first_decimal(
        getattr(risk_decision, "best_risk_reward_ratio", NA) if risk_decision is not None else NA,
        getattr(getattr(score_result, "score_breakdown", None), "best_rr", NA) if score_result is not None else NA,
        rr_to_tp2,
    )
    final_setup_valid = (
        status
        in (
            ScannerPipelineStatus.IDEA_CREATED,
            ScannerPipelineStatus.ALERT_DRY_RUN_CREATED,
            ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        )
        or trade_idea is not None
        or alert_result is not None
        or journal_entry is not None
    )
    if setup is not None:
        final_setup_valid = final_setup_valid and _is_valid_strategy_setup(setup)

    derivatives_support = _derivatives_support_for_quality(
        bias=bias,
        diagnostics=diagnostics,
        derivatives_enrichment=derivatives_enrichment,
    )
    stop_distance_pct = _stop_distance_pct(setup)
    required_rr = _required_rr_for_diagnostics(diagnostics, mode)

    return validate_setup_quality(
        SetupQualityInput(
            symbol=symbol,
            setup_valid=final_setup_valid,
            mode=mode,
            bias=bias,
            rr_to_tp2=rr_to_tp2,
            required_rr=required_rr,
            sweep_passed=_diagnostic_passed(diagnostics, "execution_sweep_status", "sweep"),
            confirmation_passed=_diagnostic_passed(diagnostics, "confirmation_structure_shift_status", "bos_choch"),
            confirmation_timeframe=_display_decimal_or_text(diagnostics.get("confirmation_timeframe")) if diagnostics else DEFAULT_CONFIRMATION_TIMEFRAME,
            pullback_valid=_pullback_valid_for_quality(diagnostics),
            ob_or_fvg_valid="ob_fvg" in gates_passed or _display_decimal_or_text(diagnostics.get("selected_zone_type")) != NA,
            fib_valid="fib_alignment" in gates_passed
            or _display_decimal_or_text(diagnostics.get("fib_alignment_status")) in ("aligned", "valid", "passed"),
            volume_confirmed="volume_confirmation" in gates_passed,
            late_pullback=failed_gate == "entry_window_expired",
            htf_2d_trend=_display_decimal_or_text(diagnostics.get("htf_2d_trend")) if diagnostics else NA,
            mtf_12h_trend=_display_decimal_or_text(diagnostics.get("mtf_12h_trend")) if diagnostics else NA,
            trend=_display_decimal_or_text(diagnostics.get("trend")) if diagnostics else NA,
            trust_percentage=_integer_or_na(diagnostics.get("trust_percentage")) if diagnostics else NA,
            poc_available=_first_non_na(
                getattr(setup, "poc", NA) if setup is not None else NA,
                diagnostics.get("poc") if diagnostics else NA,
            )
            != NA,
            value_area_available=_value_area_available(strategy_execution),
            derivatives_supports_trade=derivatives_support,
            derivatives_score=derivatives_enrichment.derivatives_score
            if derivatives_enrichment is not None
            else NA,
            funding_status=derivatives_enrichment.funding_status if derivatives_enrichment is not None else NA,
            oi_direction=derivatives_enrichment.oi_direction if derivatives_enrichment is not None else NA,
            price_oi_relationship=derivatives_enrichment.price_oi_relationship if derivatives_enrichment is not None else NA,
            crowding_risk=derivatives_enrichment.crowding_risk if derivatives_enrichment is not None else NA,
            squeeze_risk=derivatives_enrichment.squeeze_risk if derivatives_enrichment is not None else NA,
            risk_approved=risk_decision.approved if risk_decision is not None else NA,
            best_rr=best_rr,
            leverage_risk_level=risk_decision.leverage_risk.risk_level if risk_decision is not None else NA,
            data_quality_score=risk_decision.data_quality_score if risk_decision is not None else NA,
            stop_distance_pct=stop_distance_pct,
            first_failed_gate=failed_gate,
            gates_passed=gates_passed,
            gates_failed=gates_failed,
            hard_rejection_reasons=_sequence_from_diagnostics(diagnostics.get("hard_rejection_reasons"))
            if diagnostics
            else (),
            missing_data=missing_data,
            unverified_data=_strategy_quality_unverified_data(unverified_data),
            derivatives_missing_data=derivatives_enrichment.missing_data if derivatives_enrichment is not None else (),
            derivatives_unverified_data=derivatives_enrichment.unverified_data
            if derivatives_enrichment is not None
            else (),
            derivatives_warnings=derivatives_enrichment.warnings if derivatives_enrichment is not None else (),
            rejection_reason=rejection_reason or NA,
        )
    )


def _strategy_mode_from_execution(strategy_execution: _StrategyExecution) -> str:
    for values in (strategy_execution.valid_strategy_modes, strategy_execution.rejected_strategy_modes):
        if values:
            return values[0]
    return NA


def _diagnostic_passed(diagnostics: Mapping[str, Any], status_key: str, gate_name: str) -> bool:
    status = _display_decimal_or_text(diagnostics.get(status_key))
    gates_passed = _sequence_from_diagnostics(diagnostics.get("gates_passed"))
    return status == "passed" or gate_name in gates_passed


def _pullback_valid_for_quality(diagnostics: Mapping[str, Any]) -> bool:
    status = _display_decimal_or_text(diagnostics.get("pullback_zone_status"))
    return status in ("valid", "passed") or "pullback_zone" in _sequence_from_diagnostics(diagnostics.get("gates_passed"))


def _derivatives_support_for_quality(
    *,
    bias: str,
    diagnostics: Mapping[str, Any],
    derivatives_enrichment: DerivativesEnrichmentResult | None,
) -> bool | Literal["N/A"]:
    diagnostic_value = diagnostics.get("derivatives_supports_trade")
    if isinstance(diagnostic_value, bool):
        return diagnostic_value
    if _display_decimal_or_text(diagnostic_value) in ("True", "true"):
        return True
    if _display_decimal_or_text(diagnostic_value) in ("False", "false"):
        return False
    if derivatives_enrichment is None:
        return NA
    if bias == "long":
        return derivatives_enrichment.supports_long
    if bias == "short":
        return derivatives_enrichment.supports_short
    return NA


def _value_area_available(strategy_execution: _StrategyExecution) -> bool:
    profile = strategy_execution.volume_profile
    if profile is None:
        return False
    return profile.value_area_high != NA and profile.value_area_low != NA


def _stop_distance_pct(setup: LiquidityGrabSetup | None) -> MaybeDecimal:
    if setup is None or setup.entry == NA or setup.stop == NA or setup.entry == 0:
        return NA
    distance = abs(setup.entry - setup.stop) / abs(setup.entry) * Decimal("100")
    return _quantize(distance)


def _sequence_from_diagnostics(value: Any) -> tuple[str, ...]:
    if not _is_sequence_data(value):
        return ()
    return tuple(_display_decimal_or_text(item) for item in value if _display_decimal_or_text(item) != NA)


def _first_non_na(*values: Any) -> Any:
    for value in values:
        if _display_decimal_or_text(value) != NA:
            return value
    return NA


def _first_decimal(*values: Any) -> MaybeDecimal:
    for value in values:
        if _display_decimal_or_text(value) == NA:
            continue
        try:
            return _quantize(_decimal_from(value, "setup_quality"))
        except ValueError:
            continue
    return NA


def _integer_or_na(value: Any) -> MaybeInt:
    if _display_decimal_or_text(value) == NA:
        return NA
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return NA


def _latest_swing_price(points: Sequence[Any]) -> MaybeDecimal:
    if not points:
        return NA
    return _quantize(points[-1].price)


def _near_miss_intelligence_for_result(
    *,
    status: ScannerPipelineStatus,
    rejection_reason: str | None,
    strategy_execution: _StrategyExecution,
    trade_idea: TradeIdeaResult | None,
    alert_result: AlertResult | None,
    journal_entry: JournalEntryResult | None,
) -> NearMissIntelligence | None:
    if (
        status
        in (
            ScannerPipelineStatus.IDEA_CREATED,
            ScannerPipelineStatus.ALERT_DRY_RUN_CREATED,
            ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        )
        or trade_idea is not None
        or alert_result is not None
        or journal_entry is not None
    ):
        return None

    diagnostics = _representative_strategy_diagnostics(strategy_execution)
    if not diagnostics:
        return None
    failed_gate = _strategy_failed_gate(diagnostics)
    if failed_gate == NA:
        return None
    return build_near_miss_intelligence(
        failed_gate=failed_gate,
        short_reason=_strategy_short_reason(diagnostics, rejection_reason),
        diagnostics=diagnostics,
    )


def _representative_strategy_diagnostics(strategy_execution: _StrategyExecution) -> Mapping[str, Any]:
    for diagnostics in strategy_execution.strategy_diagnostics.values():
        if isinstance(diagnostics, Mapping) and _strategy_failed_gate(diagnostics) == TARGET_INTEGRITY_FAILED_GATE:
            return diagnostics
    for mode in strategy_execution.valid_strategy_modes:
        diagnostics = strategy_execution.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for mode in strategy_execution.rejected_strategy_modes:
        diagnostics = strategy_execution.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for mode in ("challenge", "swing", "scalp"):
        diagnostics = strategy_execution.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for diagnostics in strategy_execution.strategy_diagnostics.values():
        if isinstance(diagnostics, Mapping):
            return diagnostics
    return {}


def _representative_pullback_intelligence(
    diagnostics_by_mode: Mapping[str, Any],
    *,
    valid_modes: Sequence[str],
    rejected_modes: Sequence[str],
) -> PullbackIntelligenceResult | None:
    for mode in (*valid_modes, *rejected_modes, "challenge", "swing", "scalp"):
        diagnostics = diagnostics_by_mode.get(mode)
        if not isinstance(diagnostics, Mapping):
            continue
        payload = diagnostics.get("pullback_intelligence")
        if isinstance(payload, PullbackIntelligenceResult):
            return payload
        if isinstance(payload, Mapping):
            return PullbackIntelligenceResult.model_validate(payload)
        return build_pullback_intelligence(diagnostics)
    for diagnostics in diagnostics_by_mode.values():
        if isinstance(diagnostics, Mapping):
            payload = diagnostics.get("pullback_intelligence")
            if isinstance(payload, Mapping):
                return PullbackIntelligenceResult.model_validate(payload)
            return build_pullback_intelligence(diagnostics)
    return None


def _representative_target_intelligence(
    diagnostics_by_mode: Mapping[str, Any],
    *,
    valid_modes: Sequence[str],
    rejected_modes: Sequence[str],
) -> TargetIntelligenceResult | None:
    for mode in (*valid_modes, *rejected_modes, "challenge", "swing", "scalp"):
        diagnostics = diagnostics_by_mode.get(mode)
        if not isinstance(diagnostics, Mapping):
            continue
        payload = diagnostics.get("target_intelligence")
        if isinstance(payload, TargetIntelligenceResult):
            return payload
        if isinstance(payload, Mapping):
            return TargetIntelligenceResult.model_validate(payload)
    for diagnostics in diagnostics_by_mode.values():
        if isinstance(diagnostics, Mapping):
            payload = diagnostics.get("target_intelligence")
            if isinstance(payload, TargetIntelligenceResult):
                return payload
            if isinstance(payload, Mapping):
                return TargetIntelligenceResult.model_validate(payload)
    return None


def _target_integrity_decision(
    strategy_execution: _StrategyExecution,
    candidate: _CandidateSetup,
) -> _TargetIntegrityDecision:
    reasons: list[str] = []
    warnings: list[str] = []
    target_failure = NA
    target_intelligence = strategy_execution.target_intelligence

    if target_intelligence is not None:
        target_grade = _enum_text(target_intelligence.target_quality_grade)
        failure_type = _enum_text(target_intelligence.target_failure_type)
        target_failure = failure_type
        if failure_type == TargetFailureType.TARGET_INSIDE_CHOP.value:
            warnings.append(_target_block_reason(target_intelligence))
        elif target_grade == TargetQualityGrade.REJECT.value:
            reasons.append(_target_block_reason(target_intelligence))
        elif failure_type in TARGET_INTEGRITY_BLOCKING_FAILURE_TYPES:
            reasons.append(_target_block_reason(target_intelligence))

    plan_integrity = _candidate_trade_plan_integrity(candidate)
    if not _tp_sequence_valid(
        direction=candidate.direction,
        entry=candidate.entry_price,
        entry_low=candidate.entry_low,
        entry_high=candidate.entry_high,
        stop_loss=candidate.stop_loss,
        take_profit_targets=candidate.take_profit_targets,
    ):
        integrity_reason = (
            f"Trade plan integrity failed: {plan_integrity.reason}."
            if not plan_integrity.valid
            else INVALID_TP_SEQUENCE_WARNING
        )
        reasons.append(integrity_reason)
        warnings.append(integrity_reason)
        if not plan_integrity.valid:
            target_failure = plan_integrity.reason

    if reasons:
        reason = _unique_strings(reasons)[0]
        warning = _unique_strings(warnings)[0] if warnings else reason
        return _TargetIntegrityDecision(
            blocked=True,
            reason=reason,
            warning=warning,
            strategy_execution=_strategy_execution_with_target_integrity_block(
                strategy_execution,
                reason=reason,
                warning=warning,
                failure_type=target_failure,
            ),
        )

    if warnings:
        warning = _unique_strings(warnings)[0]
        return _TargetIntegrityDecision(
            blocked=False,
            warning=warning,
            strategy_execution=_strategy_execution_with_target_integrity_warning(
                strategy_execution,
                warning=warning,
                failure_type=target_failure,
            ),
        )

    return _TargetIntegrityDecision()


def _strategy_execution_with_target_integrity_block(
    strategy_execution: _StrategyExecution,
    *,
    reason: str,
    warning: str,
    failure_type: str = NA,
) -> _StrategyExecution:
    diagnostics = dict(strategy_execution.strategy_diagnostics)
    target_modes = (
        strategy_execution.valid_strategy_modes
        or strategy_execution.rejected_strategy_modes
        or tuple(diagnostics)
        or ("swing",)
    )
    for mode in target_modes:
        raw = diagnostics.get(mode)
        mode_diagnostics = dict(raw) if isinstance(raw, Mapping) else {}
        mode_diagnostics["first_failed_gate"] = TARGET_INTEGRITY_FAILED_GATE
        mode_diagnostics["target_integrity_status"] = "blocked"
        mode_diagnostics["target_integrity_reason"] = reason
        mode_diagnostics["target_integrity_warning"] = warning
        target_failure = _first_non_na(failure_type, mode_diagnostics.get("target_failure"), reason)
        mode_diagnostics["target_failure"] = target_failure
        mode_diagnostics["target_failure_type"] = target_failure
        mode_diagnostics["target_failure_severity"] = TARGET_FAILURE_SEVERITY_FATAL
        mode_diagnostics["target_warning_reason"] = warning
        mode_diagnostics["pullback_zone_status"] = _first_non_na(mode_diagnostics.get("pullback_zone_status"), "valid")
        mode_diagnostics["gates_passed"] = _sequence_from_diagnostics(mode_diagnostics.get("gates_passed"))
        gates_failed = _sequence_from_diagnostics(mode_diagnostics.get("gates_failed"))
        mode_diagnostics["gates_failed"] = _unique_strings((*gates_failed, TARGET_INTEGRITY_FAILED_GATE))
        warnings = _sequence_from_diagnostics(mode_diagnostics.get("warnings"))
        mode_diagnostics["warnings"] = _unique_strings((*warnings, warning))
        diagnostics[mode] = mode_diagnostics

    return strategy_execution.model_copy(
        update={
            "strategy_diagnostics": diagnostics,
            "valid_strategy_modes": (),
            "rejected_strategy_modes": _unique_strings((*target_modes, *strategy_execution.rejected_strategy_modes)),
        }
    )


def _strategy_execution_with_target_integrity_warning(
    strategy_execution: _StrategyExecution,
    *,
    warning: str,
    failure_type: str = NA,
) -> _StrategyExecution:
    diagnostics = dict(strategy_execution.strategy_diagnostics)
    target_modes = (
        strategy_execution.valid_strategy_modes
        or strategy_execution.rejected_strategy_modes
        or tuple(diagnostics)
        or ("swing",)
    )
    for mode in target_modes:
        raw = diagnostics.get(mode)
        mode_diagnostics = dict(raw) if isinstance(raw, Mapping) else {}
        mode_diagnostics["target_integrity_status"] = "warning"
        mode_diagnostics["target_integrity_warning"] = warning
        mode_diagnostics["target_warning_reason"] = warning
        target_failure = _first_non_na(failure_type, mode_diagnostics.get("target_failure"), warning)
        mode_diagnostics["target_failure"] = target_failure
        mode_diagnostics["target_failure_type"] = target_failure
        mode_diagnostics["target_failure_severity"] = TARGET_FAILURE_SEVERITY_SOFT
        gates_passed = _sequence_from_diagnostics(mode_diagnostics.get("gates_passed"))
        mode_diagnostics["gates_passed"] = _unique_strings((*gates_passed, TARGET_INTEGRITY_FAILED_GATE))
        warnings = _sequence_from_diagnostics(mode_diagnostics.get("warnings"))
        mode_diagnostics["warnings"] = _unique_strings((*warnings, warning))
        diagnostics[mode] = mode_diagnostics
    return strategy_execution.model_copy(update={"strategy_diagnostics": diagnostics})


def _strategy_execution_with_target_integrity_pass(strategy_execution: _StrategyExecution) -> _StrategyExecution:
    diagnostics = dict(strategy_execution.strategy_diagnostics)
    target_modes = (
        strategy_execution.valid_strategy_modes
        or strategy_execution.rejected_strategy_modes
        or tuple(diagnostics)
        or ("swing",)
    )
    for mode in target_modes:
        raw = diagnostics.get(mode)
        mode_diagnostics = dict(raw) if isinstance(raw, Mapping) else {}
        mode_diagnostics["target_integrity_status"] = _first_non_na(
            mode_diagnostics.get("target_integrity_status"),
            "passed",
        )
        mode_diagnostics["target_failure_severity"] = _first_non_na(
            mode_diagnostics.get("target_failure_severity"),
            TARGET_FAILURE_SEVERITY_PASSED,
        )
        gates_passed = _sequence_from_diagnostics(mode_diagnostics.get("gates_passed"))
        mode_diagnostics["gates_passed"] = _unique_strings((*gates_passed, TARGET_INTEGRITY_FAILED_GATE))
        diagnostics[mode] = mode_diagnostics
    return strategy_execution.model_copy(update={"strategy_diagnostics": diagnostics})


def _technical_score_for_scoring(
    technical: TechnicalStructureResult,
    strategy_execution: _StrategyExecution,
) -> int:
    base_score = int(technical.structure_score)
    diagnostics = _representative_strategy_diagnostics(strategy_execution)
    feature_score = _strategy_feature_technical_score(diagnostics)
    if feature_score is None:
        return base_score
    return max(base_score, feature_score)


def _strategy_feature_technical_score(diagnostics: Mapping[str, Any]) -> int | None:
    if not diagnostics:
        return None
    gates_passed = set(_sequence_from_diagnostics(diagnostics.get("gates_passed")))
    gates_failed = set(_sequence_from_diagnostics(diagnostics.get("gates_failed")))
    score = 10
    features_seen = 0

    sweep_status = _display_decimal_or_text(diagnostics.get("execution_sweep_status")).lower()
    if sweep_status == "passed" or "sweep" in gates_passed:
        score += 20
        features_seen += 1

    shift_status = _display_decimal_or_text(diagnostics.get("confirmation_structure_shift_status")).lower()
    if shift_status in {"passed", "valid"} or "bos_choch" in gates_passed:
        score += 25
        features_seen += 1

    pullback_status = _display_decimal_or_text(diagnostics.get("pullback_zone_status")).lower()
    if pullback_status in {"valid", "passed"} or "pullback_zone" in gates_passed:
        score += 15
        features_seen += 1

    selected_zone_type = _display_decimal_or_text(diagnostics.get("selected_zone_type"))
    if selected_zone_type != NA or "ob_fvg" in gates_passed:
        score += 15
        features_seen += 1

    rr = _first_decimal(
        diagnostics.get("rr_to_tp2"),
        diagnostics.get("best_rr"),
        diagnostics.get("planned_rr"),
        diagnostics.get("rr"),
    )
    rr_failure_gates = {"missing_rr", "rr_below_minimum", "challenge_rr_below_3"}
    required_rr = _required_rr_for_diagnostics(diagnostics)
    if rr != NA and rr >= required_rr and not bool(gates_failed & rr_failure_gates):
        score += 10
        features_seen += 1

    target_status = _display_decimal_or_text(diagnostics.get("target_integrity_status")).lower()
    target_failed = TARGET_INTEGRITY_FAILED_GATE in gates_failed or target_status in {"blocked", "failed", "reject"}
    if not target_failed and (target_status in {"passed", "valid", "ok"} or TARGET_INTEGRITY_FAILED_GATE in gates_passed):
        score += 5
        features_seen += 1

    if features_seen == 0:
        return None
    return min(score, 100)


def _required_rr_for_diagnostics(diagnostics: Mapping[str, Any], mode: Any = None) -> Decimal:
    configured = _first_decimal(
        diagnostics.get("effective_minimum_rr"),
        diagnostics.get("required_rr"),
    )
    if configured != NA:
        return configured
    mode_name = _display_decimal_or_text(mode if mode is not None else diagnostics.get("mode")).lower()
    if mode_name not in {"challenge", "scalp", "swing"}:
        mode_name = "swing"
    return hard_mode_minimum_rr(mode_name)


def _target_block_reason(target_intelligence: TargetIntelligenceResult) -> str:
    for value in (
        target_intelligence.rr_compression_reason,
        target_intelligence.next_target_condition,
        _enum_text(target_intelligence.target_failure_type),
    ):
        text = _display_decimal_or_text(value)
        if text != NA:
            return text
    return "Target integrity guard blocked alert creation because target quality is reject."


def _candidate_trade_plan_integrity(candidate: _CandidateSetup) -> TradePlanIntegrityResult:
    targets = tuple(candidate.take_profit_targets)
    return validate_trade_plan(
        direction=candidate.direction,
        entry_low=candidate.entry_low,
        entry_high=candidate.entry_high,
        entry_reference=candidate.entry_price,
        stop_loss=candidate.stop_loss,
        tp1=targets[0] if len(targets) > 0 else None,
        tp2=targets[1] if len(targets) > 1 else None,
        tp3=targets[2] if len(targets) > 2 else None,
        entry_reference_type=(
            "zone_low_limit"
            if candidate.direction == "long" and candidate.entry_price == candidate.entry_low
            else "zone_high_limit"
            if candidate.direction == "short" and candidate.entry_price == candidate.entry_high
            else "explicit_entry"
        ),
    )


def _tp_sequence_valid(
    *,
    direction: Literal["long", "short"],
    entry: Decimal,
    take_profit_targets: Sequence[Decimal],
    entry_low: Decimal | None = None,
    entry_high: Decimal | None = None,
    stop_loss: Decimal | None = None,
) -> bool:
    targets = tuple(take_profit_targets)
    point_low = entry if entry_low is None else entry_low
    point_high = entry if entry_high is None else entry_high
    synthetic_stop = (
        entry - Decimal("1")
        if direction == "long"
        else entry + Decimal("1")
    )
    return validate_trade_plan(
        direction=direction,
        entry_low=point_low,
        entry_high=point_high,
        entry_reference=entry,
        stop_loss=synthetic_stop if stop_loss is None else stop_loss,
        tp1=targets[0] if len(targets) > 0 else None,
        tp2=targets[1] if len(targets) > 1 else None,
        tp3=targets[2] if len(targets) > 2 else None,
    ).valid


def _enum_text(value: Any) -> str:
    return _display_decimal_or_text(getattr(value, "value", value))


def _strategy_failed_gate(diagnostics: Mapping[str, Any]) -> str:
    failed_gate = _display_decimal_or_text(diagnostics.get("first_failed_gate"))
    if failed_gate != NA:
        return failed_gate
    gates_failed = diagnostics.get("gates_failed")
    if _is_sequence_data(gates_failed) and gates_failed:
        return _display_decimal_or_text(gates_failed[0])
    return NA


def _strategy_short_reason(diagnostics: Mapping[str, Any], rejection_reason: str | None) -> str:
    failed_gate = _strategy_failed_gate(diagnostics)
    if failed_gate in ("missing_confirmation_structure_shift", "missing_confirmation_candles"):
        reason = _display_decimal_or_text(diagnostics.get("confirmation_bos_choch_reason"))
        if reason != NA:
            return reason

    for key in (
        "pullback_failure_reason",
        "derivatives_conflict_reason",
        "confirmation_bos_choch_reason",
        "rr_diagnostics",
        "trust_meter_diagnostics",
    ):
        reason = _display_decimal_or_text(diagnostics.get(key))
        if reason != NA:
            return reason

    hard_rejections = diagnostics.get("hard_rejection_reasons")
    if _is_sequence_data(hard_rejections) and hard_rejections:
        return _display_decimal_or_text(hard_rejections[0])
    if rejection_reason:
        return rejection_reason
    return NA


def _display_decimal_or_text(value: object) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _rejection_stage_for(status: ScannerPipelineStatus) -> str:
    stages = {
        ScannerPipelineStatus.SCAN_ERROR: "scanner",
        ScannerPipelineStatus.SCANNED_NO_SETUP: "technical",
        ScannerPipelineStatus.REJECTED_BY_TECHNICAL: "technical",
        ScannerPipelineStatus.REJECTED_BY_DERIVATIVES: "derivatives",
        ScannerPipelineStatus.REJECTED_BY_RISK: "risk",
        ScannerPipelineStatus.REJECTED_BY_SCORING: "scoring",
        ScannerPipelineStatus.REJECTED_BY_REGIME: "regime",
        ScannerPipelineStatus.FAILED: "scanner",
    }
    return stages.get(status, NA)


def _rejection_reasons_for(status: ScannerPipelineStatus, rejection_reason: str | None) -> tuple[str, ...]:
    if rejection_reason:
        return (rejection_reason,)
    if status == ScannerPipelineStatus.SCAN_ERROR:
        return ("scan_error",)
    if status in (
        ScannerPipelineStatus.IDEA_CREATED,
        ScannerPipelineStatus.ALERT_DRY_RUN_CREATED,
        ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
    ):
        return ()
    if status == ScannerPipelineStatus.SCANNED_NO_SETUP:
        return ("No deterministic setup context was detected.",)
    return ()


def _score_has_violation(result: OpportunityScoreResult | None, code: str) -> bool:
    return result is not None and any(
        violation.code == code
        for violation in result.hard_filter_result.violations
    )


def _scoring_rejection_stage(result: OpportunityScoreResult) -> str:
    if _score_has_violation(result, "weak_technical_score"):
        return "technical_invalid"
    return "scoring"


def _technical_rejection_stage(result: TechnicalStructureResult) -> str:
    if result.analysis_status == TechnicalAnalysisStatus.INSUFFICIENT_DATA:
        return "technical_insufficient_data"
    if result.analysis_status == TechnicalAnalysisStatus.DATA_ERROR:
        return "technical_data_error"
    return "technical_invalid"


def _technical_data_diagnostic(result: TechnicalStructureResult) -> str:
    timeframe = result.timeframe if result.timeframe != NA else "unknown"
    unavailable = tuple(
        component.component
        for component in result.component_availability
        if component.required and component.status != "available"
    )
    components = ",".join(unavailable) if unavailable else "all"
    reliability = (
        "N/A"
        if result.analysis_status == TechnicalAnalysisStatus.INSUFFICIENT_DATA
        else "Unverified"
    )
    return (
        f"technical_candles[{timeframe}]: {reliability} "
        f"(status={result.analysis_status.value}, component={components}, "
        f"required_bars={result.required_candles}, available_bars={result.available_candles})"
    )


def _strategy_history_diagnostics(timeframe: str, available_bars: int) -> tuple[str, str]:
    return (
        f"candles_{timeframe}: N/A",
        f"candle_sufficiency[{timeframe}]: N/A (status=INSUFFICIENT_DATA, "
        f"component=strategy, required_bars={MIN_STRATEGY_CLOSED_CANDLES}, "
        f"available_bars={available_bars})",
    )


def _strategy_data_error_diagnostics(timeframe: str) -> tuple[str, str]:
    return (
        f"candles_{timeframe}: N/A",
        f"candle_sufficiency[{timeframe}]: N/A (status=DATA_ERROR, "
        f"component=strategy, required_bars={MIN_STRATEGY_CLOSED_CANDLES}, available_bars=0)",
    )


def _technical_candles(candles: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    for candle in candles:
        output.append(
            {
                "timestamp": _field(candle, "timestamp"),
                "open": _field(candle, "open"),
                "high": _field(candle, "high"),
                "low": _field(candle, "low"),
                "close": _field(candle, "close"),
                "volume": _field(candle, "volume"),
            }
        )
    return tuple(output)


def _liquidity_grab_input(
    *,
    symbol: str,
    candles_by_timeframe: Mapping[str, Sequence[Any]],
    current_price: MaybeDecimal,
    optional_data: _OptionalMarketData,
    technical: TechnicalStructureResult,
    aggressive_toggle: bool,
    min_rr: Decimal,
    htf_timeframe: str,
    bias_timeframe: str,
    structure_timeframe: str,
    execution_timeframe: str,
    confirmation_timeframe: str,
    volume_profile: VolumeProfileResult,
    derivatives_enrichment: DerivativesEnrichmentResult | None = None,
    timeframe_context: Mapping[str, Any] | None = None,
    global_context: GlobalContextSnapshot | None = None,
    microstructure_flow: MicrostructureFlowSnapshot | None = None,
) -> dict[str, Any]:
    support_levels = _strategy_levels(technical.nearest_support, technical.recent_range_low)
    resistance_levels = _strategy_levels(technical.nearest_resistance, technical.recent_range_high)
    timeframe_context = timeframe_context or {}
    research_context = global_context.strategy_context() if global_context is not None else {}
    cvd_context = microstructure_flow.cvd_strategy_context() if microstructure_flow else None
    orderflow_context = (
        microstructure_flow.orderflow_strategy_context() if microstructure_flow else None
    )
    return {
        "symbol": symbol,
        "htf_timeframe": htf_timeframe,
        "bias_timeframe": bias_timeframe,
        "structure_timeframe": structure_timeframe,
        "execution_timeframe": execution_timeframe,
        "confirmation_timeframe": confirmation_timeframe,
        "structure_candles": candles_by_timeframe.get(structure_timeframe.strip().lower()),
        "structure_analysis_required": True,
        "candles_2d": candles_by_timeframe.get("2d"),
        "candles_12h": candles_by_timeframe.get("12h"),
        "candles_4h": candles_by_timeframe.get("4h"),
        "candles_1h": candles_by_timeframe.get("1h"),
        "candles_15m": candles_by_timeframe.get("15m"),
        "candles_5m": candles_by_timeframe.get("5m"),
        "current_price": None if current_price == NA else current_price,
        "user_support_levels": support_levels or None,
        "user_resistance_levels": resistance_levels or None,
        "poc": volume_profile.poc if volume_profile.poc != NA else NA,
        "value_area_high": volume_profile.value_area_high if volume_profile.value_area_high != NA else NA,
        "value_area_low": volume_profile.value_area_low if volume_profile.value_area_low != NA else NA,
        "volume_profile_source": volume_profile.source,
        "volume_profile_warnings": volume_profile.warnings,
        "funding": optional_data.funding,
        "open_interest": optional_data.open_interest,
        "derivatives_enrichment": derivatives_enrichment,
        "orderflow_summary": orderflow_context,
        "cvd": cvd_context,
        "liquidation_data": None,
        "btc_context": research_context.get("btc_context"),
        "btc_d_context": research_context.get("btc_d_context"),
        "weekend_filter": research_context.get("weekend_filter"),
        "min_rr": min_rr,
        "aggressive_toggle": aggressive_toggle,
        "htf_2d_context_source": timeframe_context.get("htf_2d_context_source", NA),
    }


def _volume_profile_for_timeframe(
    *,
    symbol: str,
    timeframe: str,
    candles: Sequence[Any],
) -> VolumeProfileResult:
    return calculate_volume_profile(
        VolumeProfileInput(
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
        )
    )


def _optional_12h_volume_profile(
    *,
    symbol: str,
    candles: Sequence[Any],
) -> VolumeProfileResult | None:
    if len(candles) < MIN_12H_VOLUME_PROFILE_CANDLES:
        return None
    return _volume_profile_for_timeframe(symbol=symbol, timeframe="12h", candles=candles)


def _strategy_levels(*values: MaybeDecimal) -> tuple[Decimal, ...]:
    levels: list[Decimal] = []
    for value in values:
        if value != NA:
            levels.append(_quantize(value))
    return tuple(levels)


def _synthetic_2d_source_limit(config: ScannerRunConfig, warnings: list[str]) -> int:
    requested_source_limit = _base_candle_limit(config, warnings) * 2
    return _exchange_kline_limit(
        exchange=config.exchange,
        requested_limit=requested_source_limit,
        label="1d source for synthetic 2D candles",
        warnings=warnings,
    )


def _timeframe_fetch_limit(config: ScannerRunConfig, timeframe: str, warnings: list[str]) -> int:
    normalized_timeframe = timeframe.strip().lower()
    requested_limit = _base_candle_limit(config, warnings)
    if config.replay_candles is not None and normalized_timeframe in _replay_execution_timeframes(config):
        requested_limit = _safe_replay_candles(config.replay_candles, warnings)
        if config.fast_mode and requested_limit > FAST_REPLAY_CANDLES:
            warnings.append(
                f"replay_candles limit clamped from {requested_limit} to {FAST_REPLAY_CANDLES} for fast mode."
            )
            requested_limit = FAST_REPLAY_CANDLES
    return _exchange_kline_limit(
        exchange=config.exchange,
        requested_limit=requested_limit,
        label=f"{normalized_timeframe} candles",
        warnings=warnings,
    )


def _replay_execution_timeframes(config: ScannerRunConfig) -> set[str]:
    return {
        config.execution_timeframe.strip().lower(),
        config.confirmation_timeframe.strip().lower(),
    }


def _base_candle_limit(config: ScannerRunConfig, warnings: list[str]) -> int:
    requested_limit = config.candle_limit
    if config.fast_mode and requested_limit > FAST_CANDLE_LIMIT:
        warnings.append(f"candle_limit clamped from {requested_limit} to {FAST_CANDLE_LIMIT} for fast mode.")
        return FAST_CANDLE_LIMIT
    return requested_limit


def _safe_replay_candles(replay_candles: int, warnings: list[str]) -> int:
    if replay_candles > SAFE_REPLAY_CANDLE_LIMIT_MAX:
        warnings.append(
            f"replay_candles limit clamped from {replay_candles} to {SAFE_REPLAY_CANDLE_LIMIT_MAX} "
            f"for safe replay maximum {SAFE_REPLAY_CANDLE_LIMIT_MAX}."
        )
        return SAFE_REPLAY_CANDLE_LIMIT_MAX
    return replay_candles


def _fast_mode_skips_timeframe(config: ScannerRunConfig, timeframe: str) -> bool:
    normalized = timeframe.strip().lower()
    required_timeframes = {
        config.bias_timeframe.strip().lower(),
        config.structure_timeframe.strip().lower(),
        config.execution_timeframe.strip().lower(),
        config.confirmation_timeframe.strip().lower(),
    }
    return normalized in {"4h", "1h"} and normalized not in required_timeframes


def _optional_request_timeout(config: ScannerRunConfig) -> float:
    if not config.fast_mode:
        return config.request_timeout_sec
    return min(config.request_timeout_sec, FAST_OPTIONAL_REQUEST_TIMEOUT_SEC)


def _progress_message_for_timeframe(config: ScannerRunConfig, timeframe: str) -> str | None:
    normalized = timeframe.strip().lower()
    if normalized == config.htf_timeframe.strip().lower():
        return f"Fetching HTF {normalized}..."
    if normalized == config.bias_timeframe.strip().lower():
        return f"Fetching {normalized} bias..."
    if normalized == config.structure_timeframe.strip().lower():
        return f"Fetching {normalized} structure..."
    if normalized == config.execution_timeframe.strip().lower():
        return f"Fetching {normalized} execution..."
    if normalized == config.confirmation_timeframe.strip().lower():
        return f"Fetching {normalized} confirmation..."
    return None


def _exchange_kline_limit(*, exchange: str, requested_limit: int, label: str, warnings: list[str]) -> int:
    if exchange != "binance":
        return requested_limit

    clamped = min(max(requested_limit, BINANCE_KLINE_LIMIT_MIN), BINANCE_KLINE_LIMIT_MAX)
    if clamped != requested_limit:
        warnings.append(
            f"{label} limit clamped from {requested_limit} to {clamped} "
            f"for Binance kline limit {BINANCE_KLINE_LIMIT_MIN}-{BINANCE_KLINE_LIMIT_MAX}."
        )
    return clamped


def _direct_strategy_timeframes(config: ScannerRunConfig) -> tuple[str, ...]:
    timeframes = (
        config.bias_timeframe,
        config.structure_timeframe,
        *DIRECT_STRATEGY_TIMEFRAMES,
        config.execution_timeframe,
        config.confirmation_timeframe,
    )
    normalized: list[str] = []
    for timeframe in timeframes:
        value = timeframe.strip().lower()
        if value and value not in ("2d", SYNTHETIC_2D_SOURCE_TIMEFRAME) and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _strategy_setup_for_mode(result: LiquidityGrabResult, mode: LiquidityGrabMode | str) -> LiquidityGrabSetup:
    selected = LiquidityGrabMode(mode)
    if selected == LiquidityGrabMode.challenge:
        return result.challenge
    if selected == LiquidityGrabMode.scalp:
        return result.scalp
    return result.swing


def _strategy_diagnostics_for_setup(setup: LiquidityGrabSetup) -> dict[str, Any]:
    return {
        "mode": setup.mode.value,
        "is_valid": setup.is_valid,
        "status": setup.status,
        "bias": setup.bias,
        "timeframe": setup.timeframe,
        "htf_timeframe": setup.htf_timeframe,
        "bias_timeframe": setup.bias_timeframe,
        "structure_timeframe": setup.structure_timeframe,
        "execution_timeframe": setup.execution_timeframe,
        "confirmation_timeframe": setup.confirmation_timeframe,
        "htf_2d_context_source": setup.htf_2d_context_source,
        "candles_2d_count": setup.candles_2d_count,
        "candles_12h_count": setup.candles_12h_count,
        "candles_15m_count": setup.candles_15m_count,
        "candles_5m_count": setup.candles_5m_count,
        "htf_2d_trend": setup.htf_2d_trend,
        "mtf_12h_trend": setup.mtf_12h_trend,
        "ltf_confirmation_timeframe": setup.ltf_confirmation_timeframe,
        "ltf_confirmation_status": setup.ltf_confirmation_status,
        "execution_sweep_status": setup.execution_sweep_status,
        "execution_sweep_candle_index": setup.sweep.candle_index,
        "sweep_magnitude_atr": setup.sweep.magnitude_atr,
        "confirmation_structure_shift_status": setup.confirmation_structure_shift_status,
        "confirmation_bos_choch_reason": setup.confirmation_bos_choch_reason,
        "structure_layer_analysis": setup.structure_layer_analysis.model_dump(mode="json"),
        "first_failed_gate": setup.first_failed_gate,
        "volume_profile_source": setup.volume_profile_source,
        "poc": setup.poc,
        "poc_diagnostics": setup.poc_diagnostics,
        "pullback_zone_status": setup.pullback_zone_status,
        "pullback_calculation_timeframe": setup.pullback_calculation_timeframe,
        "pullback_sweep_candle_index": setup.pullback_sweep_candle_index,
        "pullback_bos_choch_candle_index": setup.pullback_bos_choch_candle_index,
        "displacement_start_index": setup.displacement_start_index,
        "displacement_end_index": setup.displacement_end_index,
        "selected_zone_type": setup.selected_zone_type,
        "ob_zone": setup.ob_zone.model_dump(),
        "fvg_zone": setup.fvg_zone.model_dump(),
        "impulse_start": setup.pullback_zone.impulse_start,
        "impulse_end": setup.pullback_zone.impulse_end,
        "impulse_low": setup.pullback_zone.impulse_low,
        "impulse_high": setup.pullback_zone.impulse_high,
        "sweep_price": setup.pullback_zone.sweep_price,
        "bos_price": setup.pullback_zone.bos_price,
        "bos_origin_price": setup.structure_shift.level,
        "fib_alignment_status": setup.fib_alignment.status,
        "fib_382": setup.fib_382,
        "fib_618": setup.fib_618,
        "fib_65": setup.fib_65,
        "fib_786": setup.fib_786,
        "pullback_depth_ratio": setup.pullback_depth_ratio,
        "wick_close_structure": setup.wick_close_structure,
        "wick_depth_ratio": setup.wick_depth_ratio,
        "close_depth_ratio": setup.close_depth_ratio,
        "body_acceptance_ratio": setup.body_acceptance_ratio,
        "max_wick_breach": setup.max_wick_breach,
        "max_body_breach": setup.max_body_breach,
        "reclaim_detected": setup.reclaim_detected,
        "reclaim_strength": setup.reclaim_strength,
        "candles_below_fib_zone": setup.candles_below_fib_zone,
        "acceptance_status": setup.acceptance_status,
        "structural_reclaim_status": setup.structural_reclaim_status,
        "entry_low": setup.entry_low,
        "entry_high": setup.entry_high,
        "entry": setup.entry,
        "stop": setup.stop,
        "tp1": setup.tp1,
        "tp2": setup.tp2,
        "tp3": setup.tp3,
        "rr_to_tp2": setup.rr_to_tp2,
        "trade_plan_integrity": setup.pullback_zone.trade_plan_integrity,
        "trade_plan_integrity_reason": setup.pullback_zone.trade_plan_integrity_reason,
        "rr_entry_reference_type": setup.pullback_zone.rr_entry_reference_type,
        "rr_entry_reference_price": setup.pullback_zone.rr_entry_reference_price,
        "rr_target_reference": setup.pullback_zone.rr_target_reference,
        "rr_risk_distance": setup.pullback_zone.rr_risk_distance,
        "rr_reward_distance": setup.pullback_zone.rr_reward_distance,
        "configured_global_minimum_rr": setup.configured_global_minimum_rr,
        "hard_mode_floor": setup.hard_mode_floor,
        "effective_minimum_rr": setup.effective_minimum_rr,
        "candidate_rr": setup.candidate_rr,
        "rr_rejection_reason": setup.rr_rejection_reason,
        "pullback_failure_reason": setup.pullback_failure_reason,
        "trust_grade": setup.trust_meter.grade,
        "trust_percentage": setup.trust_meter.percentage,
        "gates_passed": setup.gates_passed,
        "gates_failed": setup.gates_failed,
        "hard_rejection_reasons": setup.hard_rejection_reasons,
        "sweep_diagnostics": setup.sweep_diagnostics,
        "bos_choch_diagnostics": setup.structure_shift_diagnostics,
        "ob_fvg_diagnostics": setup.ob_fvg_diagnostics,
        "pullback_zone_diagnostics": setup.pullback_zone_diagnostics,
        "fib_diagnostics": setup.fib_diagnostics,
        "rr_diagnostics": setup.rr_diagnostics,
        "trust_meter_diagnostics": setup.trust_meter_diagnostics,
        "derivatives_supports_trade": setup.derivatives_supports_trade,
        "derivatives_conflict_reason": setup.derivatives_conflict_reason,
        "funding_context": setup.funding_context,
        "oi_context": setup.oi_context,
        "crowding_risk": setup.crowding_risk,
        "squeeze_risk": setup.squeeze_risk,
        "strategy_diagnostics": setup.strategy_diagnostics,
        "missing_data": setup.missing_data,
        "unverified_data": setup.unverified_data,
    }


def _target_intelligence_for_setup(
    *,
    setup: LiquidityGrabSetup,
    diagnostics: Mapping[str, Any],
    candles_by_timeframe: Mapping[str, Sequence[Any]],
    technical: TechnicalStructureResult,
    volume_profile: VolumeProfileResult,
    higher_timeframe_volume_profile: VolumeProfileResult | None,
) -> TargetIntelligenceResult:
    calculation_timeframe = _display_decimal_or_text(diagnostics.get("pullback_calculation_timeframe"))
    execution_timeframe = _display_decimal_or_text(diagnostics.get("execution_timeframe"))
    confirmation_timeframe = _display_decimal_or_text(diagnostics.get("confirmation_timeframe"))
    candles = _target_candles_for_timeframe(
        candles_by_timeframe,
        calculation_timeframe,
        confirmation_timeframe,
        execution_timeframe,
    )
    htf_candles = _target_htf_candles(candles_by_timeframe, setup)
    profile = higher_timeframe_volume_profile or volume_profile
    mode = setup.mode.value
    required_rr = setup.effective_minimum_rr
    return build_target_intelligence(
        symbol=setup.pullback_zone.symbol if hasattr(setup.pullback_zone, "symbol") else diagnostics.get("symbol", NA),
        mode=mode,
        direction=_first_non_na(setup.bias, diagnostics.get("bias")),
        entry=_first_non_na(setup.entry, diagnostics.get("entry")),
        stop=_first_non_na(setup.stop, diagnostics.get("stop")),
        current_price=_first_non_na(setup.current_price, diagnostics.get("current_price")),
        minimum_rr=required_rr,
        candles=candles,
        htf_candles=htf_candles,
        recent_range_high=technical.recent_range_high,
        recent_range_low=technical.recent_range_low,
        nearest_support=technical.nearest_support,
        nearest_resistance=technical.nearest_resistance,
        bos_origin_price=_first_non_na(setup.structure_shift.level, diagnostics.get("bos_origin_price")),
        impulse_start=_first_non_na(setup.pullback_zone.impulse_start, diagnostics.get("impulse_start")),
        impulse_end=_first_non_na(setup.pullback_zone.impulse_end, diagnostics.get("impulse_end")),
        poc=_first_non_na(getattr(profile, "poc", NA), volume_profile.poc),
        value_area_high=_first_non_na(getattr(profile, "value_area_high", NA), volume_profile.value_area_high),
        value_area_low=_first_non_na(getattr(profile, "value_area_low", NA), volume_profile.value_area_low),
        nearest_high_volume_node=_first_non_na(
            getattr(profile, "nearest_high_volume_node", NA),
            volume_profile.nearest_high_volume_node,
        ),
        nearest_low_volume_node=_first_non_na(
            getattr(profile, "nearest_low_volume_node", NA),
            volume_profile.nearest_low_volume_node,
        ),
        user_support_levels=_strategy_levels(technical.nearest_support, technical.recent_range_low),
        user_resistance_levels=_strategy_levels(technical.nearest_resistance, technical.recent_range_high),
        missing_data=(
            *setup.missing_data,
            *_sequence_from_diagnostics(diagnostics.get("missing_data")),
        ),
        unverified_data=(
            *setup.unverified_data,
            *_sequence_from_diagnostics(diagnostics.get("unverified_data")),
        ),
    )


def _target_candles_for_timeframe(
    candles_by_timeframe: Mapping[str, Sequence[Any]],
    calculation_timeframe: str,
    confirmation_timeframe: str,
    execution_timeframe: str,
) -> Sequence[Any]:
    for timeframe in (calculation_timeframe, confirmation_timeframe, execution_timeframe, "15m"):
        normalized = _display_decimal_or_text(timeframe).lower()
        if normalized != NA.lower() and candles_by_timeframe.get(normalized):
            return candles_by_timeframe[normalized]
    return ()


def _target_htf_candles(
    candles_by_timeframe: Mapping[str, Sequence[Any]],
    setup: LiquidityGrabSetup,
) -> Sequence[Any]:
    for timeframe in (
        _display_decimal_or_text(setup.bias_timeframe).lower(),
        _display_decimal_or_text(setup.htf_timeframe).lower(),
        "12h",
        "4h",
        "1h",
    ):
        if timeframe != NA.lower() and candles_by_timeframe.get(timeframe):
            return candles_by_timeframe[timeframe]
    return ()


def _is_valid_strategy_setup(setup: LiquidityGrabSetup) -> bool:
    return setup.is_valid and setup.trust_meter.grade in ("A", "B")


def _candidate_from_strategy_setup(
    *,
    setup: LiquidityGrabSetup,
    symbol: str,
    exchange: str,
    fallback_interval: str,
) -> _CandidateSetup:
    if setup.bias not in ("long", "short"):
        raise ValueError("Valid strategy setup is missing trade bias.")
    required_levels = (setup.entry_low, setup.entry_high, setup.entry, setup.stop, setup.tp1, setup.tp2)
    if any(value == NA for value in required_levels):
        raise ValueError("Valid strategy setup is missing required trade levels.")

    take_profit_targets = tuple(
        _quantize(value)
        for value in (setup.tp1, setup.tp2, setup.tp3)
        if value != NA
    )
    technical_summary = (
        f"Liquidity-Grab Pullback {setup.mode.value} setup: {setup.structure_shift.kind} "
        f"{setup.structure_shift.direction}; OB/FVG source {setup.entry_source}; "
        f"RR to TP2 {setup.rr_to_tp2}; Trust Meter {setup.trust_meter.grade} "
        f"{setup.trust_meter.percentage}%."
    )
    confirmed_facts = _unique_strings(
        (
            setup.sweep_diagnostics,
            setup.structure_shift_diagnostics,
            setup.ob_fvg_diagnostics,
            setup.fib_diagnostics,
            setup.rr_diagnostics,
            setup.trust_meter_diagnostics,
        )
    )

    return _CandidateSetup(
        symbol=symbol,
        exchange=exchange,
        direction=setup.bias,
        timeframe=setup.timeframe if setup.timeframe != NA else fallback_interval,
        setup_type=f"liquidity_grab_pullback_{setup.mode.value}",
        entry_price=_quantize(setup.entry),
        entry_low=_quantize(setup.entry_low),
        entry_high=_quantize(setup.entry_high),
        stop_loss=_quantize(setup.stop),
        take_profit_targets=take_profit_targets,
        invalidation=setup.invalidation,
        cancel_condition="Cancel if price reaches invalidation before entry or strategy gates are no longer valid.",
        setup_location="edge" if setup.sweep.is_present else "breakout_retest",
        technical_summary=technical_summary,
        confirmed_facts=confirmed_facts,
    )


def _strategy_catalyst_score(setup: LiquidityGrabSetup | None) -> Decimal | None:
    if setup is None or not _is_valid_strategy_setup(setup):
        return None
    return Decimal(setup.trust_meter.percentage)


def _build_candidate(
    *,
    symbol: str,
    exchange: str,
    interval: str,
    candles: Sequence[Any],
    current_price: MaybeDecimal,
    technical: TechnicalStructureResult,
) -> _CandidateBuildResult:
    if current_price == NA:
        return _CandidateBuildResult(
            status=ScannerPipelineStatus.REJECTED_BY_TECHNICAL,
            reason="Current price is N/A.",
            missing_data=("current_price: N/A",),
        )

    signals = _active_directional_signals(technical)
    if not signals:
        return _CandidateBuildResult(
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            reason="No sweep, BOS, or CHoCH context was detected.",
        )

    directions = {direction for _name, direction, _level in signals}
    if len(directions) != 1:
        return _CandidateBuildResult(
            status=ScannerPipelineStatus.REJECTED_BY_TECHNICAL,
            reason="Conflicting bullish and bearish structure context.",
            missing_data=("candidate_direction: N/A",),
        )

    signal_name, signal_direction, level = _preferred_signal(signals)
    if level == NA:
        return _CandidateBuildResult(
            status=ScannerPipelineStatus.REJECTED_BY_TECHNICAL,
            reason="Required structure level is N/A.",
            missing_data=("structure_level: N/A",),
        )

    direction: Literal["long", "short"] = "long" if signal_direction == "bullish" else "short"
    stop_loss = _stop_loss_for_direction(direction, technical)
    if stop_loss == NA:
        return _CandidateBuildResult(
            status=ScannerPipelineStatus.REJECTED_BY_TECHNICAL,
            reason="Required stop-loss swing level is N/A.",
            missing_data=("stop_loss: N/A", "invalidation: N/A"),
        )

    entry_price = _quantize(current_price)
    structure_level = _quantize(level)
    entry_low = min(entry_price, structure_level)
    entry_high = max(entry_price, structure_level)
    risk_per_unit = entry_price - stop_loss if direction == "long" else stop_loss - entry_price
    if risk_per_unit <= 0:
        return _CandidateBuildResult(
            status=ScannerPipelineStatus.REJECTED_BY_TECHNICAL,
            reason="Stop loss is not on the valid side of current price.",
            missing_data=("invalidation: N/A",),
        )

    take_profits = _take_profits(direction, entry_price, risk_per_unit)
    invalidation = (
        f"Price closes below recent swing low at {stop_loss}."
        if direction == "long"
        else f"Price closes above recent swing high at {stop_loss}."
    )
    cancel_condition = (
        "Cancel if price accepts below the entry zone before trigger."
        if direction == "long"
        else "Cancel if price accepts above the entry zone before trigger."
    )
    technical_summary = _technical_summary(signal_name, signal_direction, structure_level, technical)
    setup_location = _setup_location(technical)
    setup_type = f"{signal_direction}_{signal_name}"

    return _CandidateBuildResult(
        status=ScannerPipelineStatus.IDEA_CREATED,
        candidate=_CandidateSetup(
            symbol=symbol,
            exchange=exchange,
            direction=direction,
            timeframe=interval,
            setup_type=setup_type,
            entry_price=entry_price,
            entry_low=_quantize(entry_low),
            entry_high=_quantize(entry_high),
            stop_loss=_quantize(stop_loss),
            take_profit_targets=take_profits,
            invalidation=invalidation,
            cancel_condition=cancel_condition,
            setup_location=setup_location,
            technical_summary=technical_summary,
            confirmed_facts=(technical_summary,),
        ),
    )


def _active_directional_signals(
    technical: TechnicalStructureResult,
) -> tuple[tuple[str, Literal["bullish", "bearish"], MaybeDecimal], ...]:
    signals: list[tuple[str, Literal["bullish", "bearish"], MaybeDecimal]] = []
    for name, signal in (("liquidity_sweep", technical.sweep), ("bos", technical.bos), ("choch", technical.choch)):
        if signal.is_present and signal.direction in ("bullish", "bearish"):
            signals.append((name, signal.direction, signal.level))
    return tuple(signals)


def _preferred_signal(
    signals: tuple[tuple[str, Literal["bullish", "bearish"], MaybeDecimal], ...],
) -> tuple[str, Literal["bullish", "bearish"], MaybeDecimal]:
    priority = {"liquidity_sweep": 0, "choch": 1, "bos": 2}
    return sorted(signals, key=lambda item: priority[item[0]])[0]


def _stop_loss_for_direction(direction: Literal["long", "short"], technical: TechnicalStructureResult) -> MaybeDecimal:
    swings = technical.swing_lows if direction == "long" else technical.swing_highs
    if not swings:
        return NA
    return _quantize(swings[-1].price)


def _take_profits(
    direction: Literal["long", "short"],
    entry_price: Decimal,
    risk_per_unit: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    if direction == "long":
        return (
            _quantize(entry_price + risk_per_unit * Decimal("2")),
            _quantize(entry_price + risk_per_unit * Decimal("3")),
            _quantize(entry_price + risk_per_unit * Decimal("4")),
        )
    return (
        _quantize(entry_price - risk_per_unit * Decimal("2")),
        _quantize(entry_price - risk_per_unit * Decimal("3")),
        _quantize(entry_price - risk_per_unit * Decimal("4")),
    )


def _technical_summary(
    signal_name: str,
    signal_direction: Literal["bullish", "bearish"],
    level: Decimal,
    technical: TechnicalStructureResult,
) -> str:
    readable = {
        "liquidity_sweep": "liquidity sweep",
        "bos": "break of structure",
        "choch": "change of character",
    }[signal_name]
    return (
        f"{signal_direction.title()} {readable} at {level}; "
        f"trend context {technical.trend_context}; range position {technical.range_position}."
    )


def _setup_location(technical: TechnicalStructureResult) -> Literal["edge", "middle", "breakout_retest", "unknown"]:
    if technical.sweep.is_present:
        return "edge"
    if technical.bos.is_present or technical.choch.is_present:
        return "breakout_retest"
    if technical.range_position == "middle":
        return "middle"
    return "unknown"


def _derivatives_input(
    *,
    candles: Sequence[Any],
    ticker: Any | None,
    funding: Any | None,
    open_interest: Any | None,
    previous_open_interest: MaybeDecimal,
    volume_z_score: MaybeDecimal,
) -> dict[str, Any]:
    return {
        "price_change_pct": _price_change_percentage(candles, ticker),
        "current_price": _current_price_from_candles(candles),
        "funding_rate": _decimal_field(funding, ("funding_rate", "current_funding_rate")),
        "current_open_interest": _decimal_field(open_interest, ("open_interest", "current_open_interest", "oi")),
        "previous_open_interest": previous_open_interest,
        "volume_z_score": volume_z_score,
    }


def _price_change_percentage(candles: Sequence[Any], ticker: Any | None) -> MaybeDecimal:
    ratio = _decimal_field(ticker, ("price_change_ratio_24h", "price_change_ratio", "price24hPcnt"))
    if ratio != NA:
        return _quantize(ratio * Decimal("100"))

    percentage = _decimal_field(
        ticker,
        ("price_change_24h_percentage", "price_change_percentage", "price_change_pct", "price_change_percent"),
    )
    if percentage != NA:
        return _quantize(percentage)

    if len(candles) < 2:
        return NA
    first = _decimal_field(candles[0], ("close",))
    last = _decimal_field(candles[-1], ("close",))
    if first == NA or last == NA or first == 0:
        return NA
    return _quantize((last - first) / abs(first) * Decimal("100"))


def _current_price_from_candles(candles: Sequence[Any]) -> MaybeDecimal:
    if not candles:
        return NA
    return _decimal_field(candles[-1], ("close",))


def _previous_open_interest_from(open_interest: Any | None) -> MaybeDecimal:
    return _decimal_field(open_interest, ("previous_open_interest", "previous_oi", "open_interest_previous"))


def _previous_open_interest_from_history(history: Any | None) -> MaybeDecimal:
    if _is_sequence_data(history) and len(history) >= 2:
        return _decimal_field(history[-2], ("open_interest", "current_open_interest", "oi"))
    return NA


def _derivatives_enrichment_input(
    *,
    symbol: str,
    exchange: str,
    candles: Sequence[Any],
    current_price: MaybeDecimal,
    optional_data: _OptionalMarketData,
    interval: str,
) -> DerivativesEnrichmentInput:
    return DerivativesEnrichmentInput(
        symbol=symbol,
        exchange=exchange,
        latest_price=current_price,
        current_funding_rate=_decimal_field(optional_data.funding, ("funding_rate", "current_funding_rate")),
        current_open_interest=_decimal_field(optional_data.open_interest, ("open_interest", "current_open_interest", "oi")),
        previous_open_interest=optional_data.previous_open_interest,
        candles_15m=candles if interval.strip().lower() == "15m" else (),
        funding_history=optional_data.funding_history,
        open_interest_history=optional_data.open_interest_history,
        long_short_ratio=optional_data.long_short_ratio,
        liquidation_data=optional_data.liquidation_data,
        warnings=optional_data.warnings,
    )


def _derivatives_rejection(direction: Literal["long", "short"], result: DerivativesEnrichmentResult) -> str | None:
    if direction == "long":
        if result.supports_long is False and result.crowding_risk == "high":
            return _derivatives_conflict_reason("long", result)
        if result.funding_status == "extreme_positive" and result.oi_direction == "rising":
            return _derivatives_conflict_reason("long", result)
    if direction == "short":
        if result.supports_short is False and result.crowding_risk == "high":
            return _derivatives_conflict_reason("short", result)
        if result.funding_status == "extreme_negative" and result.oi_direction == "rising":
            return _derivatives_conflict_reason("short", result)
    return None


def _derivatives_conflict_reason(direction: Literal["long", "short"], result: DerivativesEnrichmentResult) -> str:
    return (
        f"Severe derivatives conflict against {direction}: funding {result.funding_status}, "
        f"OI {result.oi_direction}, crowding {result.crowding_risk}, squeeze {result.squeeze_risk}."
    )


_MICROSTRUCTURE_UNVERIFIED_COVERAGE_REASONS = frozenset(
    {
        "aggregate_trade_id_gap_in_window",
        "connection_gap_in_window",
        "connection_reconnect_in_window",
        "trade_time_regression_in_window",
    }
)


def _microstructure_data_health_diagnostics(
    snapshot: MicrostructureFlowSnapshot,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reason = snapshot.reason or "unknown_reason"
    details = f"status={snapshot.status.value}, reason={reason}"
    observation_is_unverified = snapshot.status == ContextStatus.STALE or (
        snapshot.observed_at is not None
        and reason in _MICROSTRUCTURE_UNVERIFIED_COVERAGE_REASONS
    )
    if observation_is_unverified:
        return (), (f"microstructure_flow: Unverified ({details})",)
    return (f"microstructure_flow: N/A ({details})",), ()


def _strategy_quality_unverified_data(
    values: Sequence[str],
) -> tuple[str, ...]:
    return tuple(
        value for value in values
        if not str(value).startswith("microstructure_flow:")
    )


def _data_quality_score(
    technical: TechnicalStructureResult,
    derivatives: DerivativesOrderflowResult,
    missing_data: Sequence[str],
) -> Decimal:
    score = Decimal("100")
    if not technical.is_valid:
        score -= Decimal("50")
    if derivatives.data_quality.status == "invalid":
        score -= Decimal("50")
    elif derivatives.data_quality.status == "partial":
        score -= Decimal("15")

    penalties = {
        "funding_rate": Decimal("5"),
        "open_interest": Decimal("15"),
        "previous_open_interest": Decimal("15"),
        "volume_z_score": Decimal("5"),
        "ticker": Decimal("5"),
    }
    for label, penalty in penalties.items():
        if any(item.startswith(f"{label}:") for item in missing_data):
            score -= penalty

    if derivatives.data_quality.reliability == "Unverified":
        score -= Decimal("5")

    return _quantize(max(score, Decimal("0")))


def _liquidity_score(candles: Sequence[Any], ticker: Any | None) -> Decimal | None:
    quote_volume = _decimal_field(ticker, ("quote_volume_24h", "quote_volume"))
    volume = _decimal_field(ticker, ("volume_24h", "volume"))
    if (quote_volume != NA and quote_volume > 0) or (volume != NA and volume > 0):
        return Decimal("100")
    candle_volumes = tuple(_decimal_field(candle, ("volume",)) for candle in candles)
    if candle_volumes and all(volume != NA and volume > 0 for volume in candle_volumes):
        return Decimal("80")
    return None


def _risk_rejection_reason(result: RiskDecision) -> str:
    if result.violations:
        return "; ".join(violation.message for violation in result.violations)
    return "Risk manager rejected the setup."


def _scoring_rejection_reason(result: OpportunityScoreResult, minimum_score: Decimal) -> str:
    reasons = list(result.rejection_reasons)
    if result.total_score < minimum_score:
        reasons.append(f"Opportunity score {result.total_score} is below scanner minimum {minimum_score}.")
    if reasons:
        return "; ".join(reasons)
    return "Opportunity scoring rejected the setup."


def _trade_idea_rejection_reason(result: TradeIdeaResult) -> str:
    if result.quality_gate_result.violations:
        return "; ".join(violation.message for violation in result.quality_gate_result.violations)
    return "Trade idea quality gate rejected the setup."


def _qualification_rr_for_scoring(
    strategy_execution: _StrategyExecution,
    result: RiskDecision,
) -> Decimal:
    setup = strategy_execution.selected_setup
    if setup is not None and setup.rr_to_tp2 != NA:
        return _decimal_from(setup.rr_to_tp2, "setup.rr_to_tp2")
    diagnostics = _representative_strategy_diagnostics(strategy_execution)
    diagnostic_rr = _first_decimal(diagnostics.get("rr_to_tp2"))
    if diagnostic_rr != NA:
        return diagnostic_rr
    if result.best_risk_reward_ratio == NA:
        return Decimal("0")
    return result.best_risk_reward_ratio


def _derivatives_summary(result: DerivativesOrderflowResult) -> str:
    relationship = result.price_oi_relationship.classification
    if relationship == NA:
        relationship = "price/OI relationship N/A"
    flags = ", ".join(result.active_risk_flags) if result.active_risk_flags else "no active derivatives risk flags"
    return f"{relationship}; derivatives context score {result.derivatives_score}; {flags}."


def _derivatives_enrichment_summary(result: DerivativesEnrichmentResult) -> str:
    return (
        f"Funding {result.funding_status} ({_display_decimal(result.funding_rate)}); "
        f"OI {result.oi_direction} ({_display_decimal(result.open_interest_change_pct)}%); "
        f"Price/OI {result.price_oi_relationship}; "
        f"crowding {result.crowding_risk}; squeeze {result.squeeze_risk}; "
        f"derivatives context score {result.derivatives_score}."
    )


def _scoring_derivatives_score(result: DerivativesEnrichmentResult) -> Decimal:
    if result.derivatives_score == NA:
        return MIN_DERIVATIVES_SCORE
    score = Decimal(result.derivatives_score)
    if score < MIN_DERIVATIVES_SCORE and result.missing_data:
        return MIN_DERIVATIVES_SCORE
    return score


def _missing_data_from_derivatives(result: DerivativesOrderflowResult) -> tuple[str, ...]:
    return tuple(f"{field}: N/A" for field in result.data_quality.missing_fields)


def _unverified_data_from_derivatives(result: DerivativesOrderflowResult) -> tuple[str, ...]:
    values = [f"{field}: Unverified" for field in result.data_quality.unverified_fields]
    if result.data_quality.reliability == "Unverified":
        values.append("derivatives: Unverified")
    return tuple(values)


def _market_regime_for_scan(
    config: ScannerRunConfig,
    results: Sequence[ScannerSymbolResult],
    *,
    market_regime_context: Mapping[str, Any],
) -> MarketRegimeResult:
    if not config.market_regime_enabled:
        return disabled_market_regime_result()
    breadth = _market_regime_breadth(results)
    return evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=market_regime_context.get("btc_candles", ()),
            eth_candles=market_regime_context.get("eth_candles", ()),
            candle_timeframe=market_regime_context.get("candle_timeframe", config.bias_timeframe),
            decision_timestamp=market_regime_context.get("decision_timestamp", config.decision_timestamp),
            scanned_symbols=len(results),
            bullish_bias_pct=breadth["bullish_bias_pct"],
            bearish_bias_pct=breadth["bearish_bias_pct"],
            valid_sweep_pct=breadth["valid_sweep_pct"],
            confirmation_pct=breadth["confirmation_pct"],
            failed_confirmation_pct=breadth["failed_confirmation_pct"],
            htf_agreement_pct=breadth["htf_agreement_pct"],
            htf_conflict_pct=breadth["htf_conflict_pct"],
            average_rr=breadth["average_rr"],
            setup_density_pct=breadth["setup_density_pct"],
            rejection_clustering_pct=breadth["rejection_clustering_pct"],
            broad_participation_pct=breadth["broad_participation_pct"],
            risk_mode=config.regime_risk_mode,
            strictness=_regime_strictness_for_config(config),
            missing_data=_sequence_from_diagnostics(market_regime_context.get("missing_data")),
        )
    )


def _market_regime_breadth(results: Sequence[ScannerSymbolResult]) -> dict[str, MaybeDecimal]:
    completed = [result for result in results if not _is_scan_error(result)]
    if not completed:
        return {
            "bullish_bias_pct": NA,
            "bearish_bias_pct": NA,
            "valid_sweep_pct": NA,
            "confirmation_pct": NA,
            "failed_confirmation_pct": NA,
            "htf_agreement_pct": NA,
            "htf_conflict_pct": NA,
            "average_rr": NA,
            "setup_density_pct": NA,
            "rejection_clustering_pct": NA,
            "broad_participation_pct": NA,
        }

    bullish = bearish = valid_sweep = confirmed = failed_confirmation = 0
    htf_agreement = htf_conflict = setup_density = rejection_cluster = 0
    rr_values: list[Decimal] = []
    for result in completed:
        diagnostics = _symbol_representative_diagnostics(result)
        bias = _symbol_context_bias(result, diagnostics)
        if bias == "bullish":
            bullish += 1
        elif bias == "bearish":
            bearish += 1

        sweep_passed = _symbol_diagnostic_passed(result, diagnostics, "execution_sweep_status", result.sweep_detected)
        confirmation_passed = _symbol_diagnostic_passed(
            result,
            diagnostics,
            "confirmation_structure_shift_status",
            result.bos_detected or result.choch_detected,
        )
        if sweep_passed:
            valid_sweep += 1
        if confirmation_passed:
            confirmed += 1
        if sweep_passed and not confirmation_passed:
            failed_confirmation += 1
        if _symbol_htf_agrees_with_bias(diagnostics, bias):
            htf_agreement += 1
        elif _symbol_htf_conflicts_with_bias(diagnostics, bias):
            htf_conflict += 1
        if result.valid_strategy_modes or _diagnostic_has_setup_progress(result, diagnostics):
            setup_density += 1
        if result.rejected_strategy_modes or _diagnostic_has_late_rejection(diagnostics):
            rejection_cluster += 1
        rr = _decimal_or_na(diagnostics.get("rr_to_tp2"))
        if rr != NA:
            rr_values.append(rr)

    total = Decimal(len(completed))
    participating = max(bullish, bearish)
    return {
        "bullish_bias_pct": _percentage(bullish, total),
        "bearish_bias_pct": _percentage(bearish, total),
        "valid_sweep_pct": _percentage(valid_sweep, total),
        "confirmation_pct": _percentage(confirmed, total),
        "failed_confirmation_pct": _percentage(failed_confirmation, total),
        "htf_agreement_pct": _percentage(htf_agreement, total),
        "htf_conflict_pct": _percentage(htf_conflict, total),
        "average_rr": _average_decimal_or_na(rr_values),
        "setup_density_pct": _percentage(setup_density, total),
        "rejection_clustering_pct": _percentage(rejection_cluster, total),
        "broad_participation_pct": _percentage(participating, total),
    }


def _regime_strictness_for_config(config: ScannerRunConfig) -> RegimeStrictness:
    if config.regime_risk_mode == "conservative":
        return RegimeStrictness.HIGH
    if config.regime_risk_mode == "aggressive":
        return RegimeStrictness.LOW
    return RegimeStrictness(config.regime_strictness)


def _symbol_htf_agrees_with_bias(diagnostics: Mapping[str, Any], bias: str) -> bool:
    if bias not in ("bullish", "bearish"):
        return False
    trends = (
        _display_decimal_or_text(diagnostics.get("htf_2d_trend")),
        _display_decimal_or_text(diagnostics.get("mtf_12h_trend")),
    )
    evaluated = [trend for trend in trends if trend in ("bullish", "bearish")]
    return bool(evaluated) and all(trend == bias for trend in evaluated)


def _symbol_htf_conflicts_with_bias(diagnostics: Mapping[str, Any], bias: str) -> bool:
    if bias not in ("bullish", "bearish"):
        return False
    opposite = "bearish" if bias == "bullish" else "bullish"
    trends = (
        _display_decimal_or_text(diagnostics.get("htf_2d_trend")),
        _display_decimal_or_text(diagnostics.get("mtf_12h_trend")),
    )
    return any(trend == opposite for trend in trends)


def _diagnostic_has_setup_progress(result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> bool:
    if result.trade_idea is not None:
        return True
    gates_passed = set(_sequence_from_diagnostics(diagnostics.get("gates_passed")))
    if {"sweep", "bos_choch"} & gates_passed:
        return True
    return _display_decimal_or_text(diagnostics.get("pullback_zone_status")) in ("valid", "passed")


def _diagnostic_has_late_rejection(diagnostics: Mapping[str, Any]) -> bool:
    gate = _display_decimal_or_text(diagnostics.get("first_failed_gate"))
    gates_failed = set(_sequence_from_diagnostics(diagnostics.get("gates_failed")))
    late_gates = {
        "no_ob_or_fvg_zone",
        "pullback_too_deep",
        "pullback_beyond_786",
        "wick_sweep_reclaim",
        "body_acceptance_failure",
        "structural_breakdown",
        "missing_rr",
        "rr_below_minimum",
        "trust_meter_below_minimum",
        "challenge_trust_below_85",
        "derivatives_conflict",
        "funding_oi_guard",
        "regime_compatibility",
    }
    return gate in late_gates or bool(gates_failed & late_gates)


def _decimal_or_na(value: Any) -> MaybeDecimal:
    if _display_decimal_or_text(value) == NA:
        return NA
    try:
        return _quantize(_decimal_from(value, "market_regime_breadth"))
    except ValueError:
        return NA


def _average_decimal_or_na(values: Sequence[Decimal]) -> MaybeDecimal:
    if not values:
        return NA
    return _quantize(sum(values, Decimal("0")) / Decimal(len(values)))


def _apply_market_regime_to_results(
    results: Sequence[ScannerSymbolResult],
    market_regime: MarketRegimeResult,
) -> tuple[ScannerSymbolResult, ...]:
    if not market_regime.enabled:
        return tuple(results)
    return tuple(_apply_market_regime_to_symbol(result, market_regime) for result in results)


def _apply_market_regime_to_symbol(
    result: ScannerSymbolResult,
    market_regime: MarketRegimeResult,
) -> ScannerSymbolResult:
    if result.status == ScannerPipelineStatus.NOT_RUN:
        return result
    warnings = _symbol_regime_warnings(result, market_regime)
    setup_quality = result.setup_quality
    mode = _symbol_mode_for_regime(result)
    compatibility = _compatibility_for_mode(market_regime, mode)
    update: dict[str, Any] = {
        "setup_quality": setup_quality,
        "regime_warnings": warnings,
        "regime_state": market_regime.state.value,
        "regime_confidence_score": market_regime.confidence_score,
        "regime_penalty": market_regime.adjustment.regime_penalty,
        "regime_notes": market_regime.environment_notes,
        "regime_diagnostics": _regime_diagnostics_payload(market_regime, mode),
    }
    if compatibility is not None:
        update["regime_compatibility_score"] = compatibility.score
        update["regime_compatibility_label"] = compatibility.label
    if mode != NA and result.strategy_diagnostics:
        update["strategy_diagnostics"] = _strategy_diagnostics_with_regime_overlay(result, mode, market_regime, compatibility)
    if not warnings and setup_quality == result.setup_quality:
        unchanged_keys = {
            "regime_state",
            "regime_confidence_score",
            "regime_penalty",
            "regime_notes",
            "regime_diagnostics",
            "regime_compatibility_score",
            "regime_compatibility_label",
        }
        if all(getattr(result, key) == value for key, value in update.items() if key in unchanged_keys):
            return result
    return result.model_copy(update=update)


def _symbol_regime_warnings(
    result: ScannerSymbolResult,
    market_regime: MarketRegimeResult,
) -> tuple[str, ...]:
    if market_regime.risk_level == RegimeRiskLevel.NA:
        return ()
    if not _symbol_has_actionable_setup(result):
        return ()
    adjustment = market_regime.adjustment
    mode = _symbol_mode_for_regime(result)
    compatibility = _compatibility_for_mode(market_regime, mode)
    warnings: list[str] = []
    if market_regime.risk_level in (RegimeRiskLevel.HIGH, RegimeRiskLevel.EXTREME):
        warnings.append(
            f"Market climate {market_regime.state.value} risk {market_regime.risk_level.value}: {adjustment.explanation}"
        )
    if mode != NA and compatibility is not None and not compatibility.allowed:
        warnings.append(_regime_block_reason(market_regime, mode, compatibility))
    elif mode != NA and not _mode_allowed_by_regime(mode, adjustment):
        warnings.append(f"Market climate warns that {mode} setups are less suitable: {adjustment.explanation}")
    elif adjustment.risk_multiplier < Decimal("1"):
        warnings.append(f"Market climate risk multiplier {adjustment.risk_multiplier} is informational only: {adjustment.explanation}")
    return _unique_strings(warnings)


def _compatibility_for_mode(market_regime: MarketRegimeResult, mode: str) -> Any | None:
    if mode == NA:
        return None
    return market_regime.compatibility_scores.get(mode.lower())


def _regime_block_reason(market_regime: MarketRegimeResult, mode: str, compatibility: Any | None) -> str:
    if compatibility is None:
        return f"Market climate warning: {market_regime.adjustment.explanation}"
    conflicting = "; ".join(compatibility.notes) if compatibility.notes else market_regime.adjustment.explanation
    return (
        f"Market climate warning: {mode} compatibility is {compatibility.label} ({compatibility.score}/100). "
        f"{conflicting}"
    )


def _regime_diagnostics_payload(market_regime: MarketRegimeResult, mode: str) -> dict[str, Any]:
    compatibility = _compatibility_for_mode(market_regime, mode)
    payload: dict[str, Any] = {
        "state": market_regime.state.value,
        "confidence_score": market_regime.confidence_score,
        "confidence_band": market_regime.confidence_band.value,
        "risk_level": market_regime.risk_level.value,
        "strictness": market_regime.strictness.value,
        "regime_penalty": market_regime.adjustment.regime_penalty,
        "portfolio_confidence_adjustment": market_regime.adjustment.portfolio_confidence_adjustment,
        "notes": list(market_regime.environment_notes),
        "boosts": list(market_regime.boosts),
        "penalties": list(market_regime.penalties),
    }
    if compatibility is not None:
        payload["mode"] = compatibility.mode
        payload["compatibility"] = compatibility.model_dump(mode="json")
    return payload


def _strategy_diagnostics_with_regime_block(
    result: ScannerSymbolResult,
    mode: str,
    reason: str,
    compatibility: Any | None,
) -> dict[str, Any]:
    diagnostics_by_mode: dict[str, Any] = dict(result.strategy_diagnostics)
    selected = diagnostics_by_mode.get(mode)
    if not isinstance(selected, Mapping):
        selected = _symbol_representative_diagnostics(result)
    selected_dict = dict(selected) if isinstance(selected, Mapping) else {}
    gates_failed = _unique_strings((*_sequence_from_diagnostics(selected_dict.get("gates_failed")), "regime_compatibility"))
    hard_rejections = _unique_strings((*_sequence_from_diagnostics(selected_dict.get("hard_rejection_reasons")), reason))
    selected_dict.update(
        {
            "is_valid": False,
            "first_failed_gate": "regime_compatibility",
            "gates_failed": gates_failed,
            "hard_rejection_reasons": hard_rejections,
            "regime_compatibility_status": "failed",
            "regime_compatibility_reason": reason,
            "regime_compatibility_score": getattr(compatibility, "score", NA),
            "regime_compatibility_label": getattr(compatibility, "label", NA),
        }
    )
    if mode != NA:
        diagnostics_by_mode[mode] = selected_dict
    return diagnostics_by_mode


def _strategy_diagnostics_with_regime_overlay(
    result: ScannerSymbolResult,
    mode: str,
    market_regime: MarketRegimeResult,
    compatibility: Any | None,
) -> dict[str, Any]:
    diagnostics_by_mode: dict[str, Any] = dict(result.strategy_diagnostics)
    selected = diagnostics_by_mode.get(mode)
    if not isinstance(selected, Mapping):
        return diagnostics_by_mode
    selected_dict = dict(selected)
    trust_percentage = _integer_or_na(selected_dict.get("trust_percentage"))
    adjusted_trust: Any = NA
    if trust_percentage != NA:
        adjusted_trust = max(0, min(100, int(trust_percentage) + market_regime.adjustment.trust_score_adjustment))
    edge = _display_decimal_or_text(getattr(result.setup_quality, "profitability_edge_score", NA))
    selected_dict.update(
        {
            "regime_state": market_regime.state.value,
            "regime_confidence_score": market_regime.confidence_score,
            "regime_confidence_band": market_regime.confidence_band.value,
            "regime_penalty": market_regime.adjustment.regime_penalty,
            "regime_readiness_adjustment": market_regime.adjustment.readiness_score_adjustment,
            "regime_edge_adjustment": market_regime.adjustment.edge_score_adjustment,
            "regime_trust_adjustment": market_regime.adjustment.trust_score_adjustment,
            "regime_adjusted_trust_percentage": adjusted_trust,
            "regime_adjusted_edge_score": edge,
            "regime_compatibility_status": "passed" if compatibility is None or compatibility.allowed else "failed",
            "regime_compatibility_score": getattr(compatibility, "score", NA),
            "regime_compatibility_label": getattr(compatibility, "label", NA),
            "regime_compatibility_notes": tuple(getattr(compatibility, "notes", ())),
        }
    )
    diagnostics_by_mode[mode] = selected_dict
    return diagnostics_by_mode


def _adjust_setup_quality_for_regime(
    quality: SetupQualityResult,
    result: ScannerSymbolResult,
    market_regime: MarketRegimeResult,
) -> SetupQualityResult:
    # Regime compatibility is informational only; keep the original setup-quality state.
    return quality


def _regime_adjusted_grade(state: SetupQualityState, quality_score: int) -> SetupQualityGrade:
    if state == SetupQualityState.DATA_ISSUE:
        return SetupQualityGrade.NA
    if state == SetupQualityState.REJECTED_NO_EDGE:
        return SetupQualityGrade.REJECT
    if quality_score >= 90:
        return SetupQualityGrade.A_PLUS
    if quality_score >= 85:
        return SetupQualityGrade.A
    if quality_score >= 80:
        return SetupQualityGrade.A_MINUS
    if quality_score >= 75:
        return SetupQualityGrade.B_PLUS
    if quality_score >= 65:
        return SetupQualityGrade.B
    if quality_score >= 55:
        return SetupQualityGrade.B_MINUS
    if quality_score >= 50:
        return SetupQualityGrade.C
    return SetupQualityGrade.REJECT


def _symbol_has_actionable_setup(result: ScannerSymbolResult) -> bool:
    return (
        result.trade_idea is not None
        or bool(result.valid_strategy_modes)
        or result.setup_quality.quality_state
        in (
            SetupQualityState.HIGH_QUALITY_TRADE,
            SetupQualityState.VALID_BUT_LOWER_QUALITY,
            SetupQualityState.WATCHLIST_NEAR_MISS,
        )
    )


def _symbol_mode_for_regime(result: ScannerSymbolResult) -> str:
    if result.valid_strategy_modes:
        return result.valid_strategy_modes[0]
    if result.rejected_strategy_modes:
        return result.rejected_strategy_modes[0]
    diagnostics = _symbol_representative_diagnostics(result)
    mode = _display_decimal_or_text(diagnostics.get("mode"))
    if mode != NA:
        return mode.lower()
    return NA


def _mode_allowed_by_regime(mode: str, adjustment: RegimeAdjustment) -> bool:
    # Regime compatibility is advisory only; never suppress a valid setup by mode.
    return True


def _symbol_representative_diagnostics(result: ScannerSymbolResult) -> Mapping[str, Any]:
    for mode in result.valid_strategy_modes:
        diagnostics = result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for mode in result.rejected_strategy_modes:
        diagnostics = result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for mode in ("challenge", "swing", "scalp"):
        diagnostics = result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for diagnostics in result.strategy_diagnostics.values():
        if isinstance(diagnostics, Mapping):
            return diagnostics
    return {}


def _symbol_context_bias(result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    trends = (
        _display_decimal_or_text(diagnostics.get("htf_2d_trend")),
        _display_decimal_or_text(diagnostics.get("mtf_12h_trend")),
        result.trend_context,
    )
    bullish = sum(1 for trend in trends if trend == "bullish")
    bearish = sum(1 for trend in trends if trend == "bearish")
    if bullish > bearish:
        return "bullish"
    if bearish > bullish:
        return "bearish"
    return NA


def _symbol_diagnostic_passed(
    result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    key: str,
    fallback: bool,
) -> bool:
    status = _display_decimal_or_text(diagnostics.get(key))
    if status == "passed":
        return True
    if status in ("failed", "not_evaluated"):
        return False
    return fallback


def _percentage(count: int, total: Decimal) -> Decimal:
    if total <= 0:
        return Decimal("0")
    return _quantize(Decimal(count) / total * Decimal("100"))


def _scoring_missing_data(values: Sequence[str]) -> tuple[str, ...]:
    labels = []
    for value in values:
        label = value.split(":", 1)[0].strip()
        if label:
            labels.append(label)
    return _unique_strings(labels)


def _scoring_unverified_data(values: Sequence[str]) -> tuple[str, ...]:
    labels = []
    for value in values:
        label = value.split(":", 1)[0].strip()
        if label:
            labels.append(label)
    return _unique_strings(labels)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _emit_progress(progress: Callable[[str], Any] | None, message: str | None) -> None:
    if progress is None or not message:
        return
    await _maybe_await(progress(message))


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "N/A"
    if value < 1:
        formatted = f"{value:.2f}"
    else:
        formatted = f"{value:.1f}"
    return formatted.rstrip("0").rstrip(".")


def _is_scan_error(result: ScannerSymbolResult) -> bool:
    return result.status in (ScannerPipelineStatus.SCAN_ERROR, ScannerPipelineStatus.FAILED)


def _not_run_result(symbol: str, *, reason: str) -> ScannerSymbolResult:
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


def _scan_error_result(
    symbol: str,
    reason: str,
    *,
    runtime_seconds: float | None = None,
    timeout_status: Literal["none", "request_timeout", "symbol_timeout", "global_timeout"] = "none",
) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCAN_ERROR,
        status_history=(ScannerPipelineStatus.SCAN_ERROR,),
        error_message=reason,
        rejection_reason=reason,
        runtime_seconds=_rounded_seconds(runtime_seconds) if runtime_seconds is not None else None,
        timed_out=timeout_status != "none",
        timeout_status=timeout_status,
        rejection_stage="scanner",
        rejection_reasons=(reason,),
        setup_quality=validate_setup_quality(
            SetupQualityInput(
                symbol=symbol,
                first_failed_gate="scanner_error",
                missing_data=("scanner: N/A",),
                rejection_reason=reason,
            )
        ),
    )


def _with_symbol_runtime(
    result: ScannerSymbolResult,
    *,
    runtime_seconds: float,
    timeout_status: Literal["none", "request_timeout", "symbol_timeout", "global_timeout"],
) -> ScannerSymbolResult:
    rounded_runtime = _rounded_seconds(runtime_seconds)
    if result.runtime_seconds == rounded_runtime and result.timeout_status == timeout_status:
        return result
    return result.model_copy(
        update={
            "runtime_seconds": rounded_runtime,
            "timed_out": timeout_status != "none",
            "timeout_status": timeout_status,
        }
    )


def _safe_process_memory_reading(
    sampler: Callable[[], ProcessMemoryReading],
) -> ProcessMemoryReading:
    try:
        reading = sampler()
    except Exception as exc:
        return ProcessMemoryReading(
            rss_bytes=None,
            source=NA,
            error_code=type(exc).__name__,
        )
    if not isinstance(reading, ProcessMemoryReading):
        return ProcessMemoryReading(
            rss_bytes=None,
            source=NA,
            error_code="invalid_reading_type",
        )
    rss_bytes = reading.rss_bytes
    if rss_bytes is not None and (
        isinstance(rss_bytes, bool) or not isinstance(rss_bytes, int) or rss_bytes < 0
    ):
        return ProcessMemoryReading(
            rss_bytes=None,
            source=reading.source,
            error_code="invalid_rss_value",
        )
    return reading


def _process_memory_stats_for(
    readings: Sequence[ProcessMemoryReading],
) -> ScannerProcessMemoryStats:
    successful = tuple(reading for reading in readings if reading.rss_bytes is not None)
    sources = tuple(dict.fromkeys(reading.source for reading in readings if reading.source != NA))
    failures = tuple(reading for reading in readings if reading.rss_bytes is None)
    failure_codes = tuple(
        dict.fromkeys(reading.error_code or "unknown_error" for reading in failures)
    )
    if not successful:
        measurement_status: Literal["Verified", "Unverified", "N/A"] = NA
    elif failures or len(sources) != 1:
        measurement_status = "Unverified"
    else:
        measurement_status = "Verified"

    start_rss = readings[0].rss_bytes if readings and readings[0].rss_bytes is not None else NA
    end_rss = readings[-1].rss_bytes if readings and readings[-1].rss_bytes is not None else NA
    rss_delta = (
        end_rss - start_rss
        if isinstance(start_rss, int) and isinstance(end_rss, int)
        else NA
    )
    return ScannerProcessMemoryStats(
        measurement_status=measurement_status,
        source=";".join(sources) if sources else NA,
        rss_start_bytes=start_rss,
        rss_end_bytes=end_rss,
        rss_observed_peak_bytes=(
            max(reading.rss_bytes for reading in successful)
            if successful
            else NA
        ),
        rss_delta_bytes=rss_delta,
        samples_attempted=len(readings),
        samples_succeeded=len(successful),
        samples_failed=len(failures),
        failure_codes=failure_codes,
    )



def _runtime_stats_for(
    results: Sequence[ScannerSymbolResult],
    *,
    total_symbols: int,
    total_runtime_seconds: float,
    global_timeout_hit: bool,
    process_memory: ScannerProcessMemoryStats,
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
    return ScannerRuntimeStats(
        total_runtime_seconds=_rounded_seconds(total_runtime_seconds),
        average_seconds_per_symbol=_rounded_seconds(sum(seconds for _symbol, seconds in runtimes) / len(runtimes))
        if runtimes
        else 0.0,
        slowest_symbol=slowest_symbol,
        slowest_symbol_seconds=_rounded_seconds(slowest_seconds),
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
        process_memory=process_memory,
    )


def _rounded_seconds(value: float) -> float:
    return round(max(value, 0.0), 3)


def _clean_error_message(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return text
    return exc.__class__.__name__


def _cache_stats_for(client: BaseExchangeClient, config: ScannerRunConfig) -> dict[str, Any]:
    stats = getattr(client, "cache_stats", None)
    if callable(stats):
        return dict(stats())
    return {
        "enabled": config.cache_enabled,
        "file_cache_enabled": config.cache_file is not None and config.cache_enabled,
        "file_path": str(config.cache_file) if config.cache_file is not None else None,
        "hits": 0,
        "misses": 0,
        "expired": 0,
        "writes": 0,
        "errors": 0,
        "entries": 0,
    }


def _retry_diagnostics_for(client: BaseExchangeClient) -> tuple[dict[str, Any], ...]:
    events = getattr(client, "retry_events", None)
    if callable(events):
        return tuple(dict(event) for event in events())
    return ()


def _decimal_field(source: Any | None, names: Sequence[str]) -> MaybeDecimal:
    value = None
    for name in names:
        value = _field(source, name)
        if value is not None and value != "" and value != NA:
            break
    else:
        return NA

    try:
        return _quantize(_decimal_from(value, names[0]))
    except ValueError:
        return NA


def _field(source: Any | None, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _is_sequence_data(value: Any | None) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _normalize_optional_decimal(value: Any | None) -> MaybeDecimal:
    if value is None or value == "" or value == NA:
        return NA
    if isinstance(value, Mapping):
        for name in ("long_short_ratio", "longShortRatio", "ratio"):
            candidate = value.get(name)
            if candidate not in (None, "", NA):
                value = candidate
                break
    try:
        return _quantize(_decimal_from(value, "optional_decimal"))
    except ValueError:
        return NA


def _display_decimal(value: object) -> str:
    if value == NA or value is None:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid decimal at {path}: {value!r}") from exc
    if not decimal.is_finite():
        raise ValueError(f"Invalid decimal at {path}: {value!r}")
    return decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in output:
            output.append(cleaned)
    return tuple(output)


__all__ = [
    "DEFAULT_REPLAY_CANDLES",
    "DEFAULT_REQUEST_TIMEOUT_SEC",
    "DEFAULT_SYMBOL_TIMEOUT_SEC",
    "FAST_CANDLE_LIMIT",
    "FAST_OPTIONAL_REQUEST_TIMEOUT_SEC",
    "FAST_REPLAY_CANDLES",
    "ScannerPipelineStatus",
    "ScannerProcessMemoryStats",
    "ScannerRiskConfig",
    "ScannerRuntimeStats",
    "ScannerRunConfig",
    "ScannerRunResult",
    "ScannerRunner",
    "ScannerSymbolConfig",
    "ScannerSymbolResult",
    "SAFE_REPLAY_CANDLE_LIMIT_MAX",
]
