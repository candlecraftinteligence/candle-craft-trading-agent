from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.alert_agent import AlertAgent, AlertResult
from app.agents.derivatives_orderflow import DerivativesOrderflowAgent, DerivativesOrderflowResult
from app.agents.journal_agent import JournalAgent, JournalEntryResult, JournalStatus
from app.agents.risk_manager import RiskDecision, RiskManagerAgent, RiskManagerInput
from app.agents.technical_structure import TechnicalStructureAgent, TechnicalStructureResult
from app.agents.trade_idea import TradeIdeaAgent, TradeIdeaResult
from app.data.dtos import NA, CandleDTO, FundingDTO, MaybeDecimal, MaybeInt, OpenInterestDTO, TickerDTO
from app.data.exchange_clients import BaseExchangeClient, BinanceFuturesClient, BybitLinearClient
from app.data.timeframes import resample_ohlcv_candles
from app.scoring.opportunity_scoring import OpportunityScoreResult, OpportunityScoringEngine
from app.strategies.liquidity_grab_pullback import (
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
DIRECT_STRATEGY_TIMEFRAMES = ("12h", "4h", "1h", "15m", "5m")
SYNTHETIC_2D_SOURCE_TIMEFRAME = "1d"
NO_VALID_STRATEGY_SETUP_REASON = "No valid Liquidity-Grab Pullback setup."


class ScannerPipelineStatus(str, Enum):
    SCANNED_NO_SETUP = "scanned_no_setup"
    REJECTED_BY_TECHNICAL = "rejected_by_technical"
    REJECTED_BY_DERIVATIVES = "rejected_by_derivatives"
    REJECTED_BY_RISK = "rejected_by_risk"
    REJECTED_BY_SCORING = "rejected_by_scoring"
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
    dry_run_alerts: bool = True
    account_equity: Decimal
    risk_per_trade_pct: Decimal
    max_daily_risk_pct: Decimal | None = None
    current_daily_loss_pct: Decimal | None = None
    leverage: Decimal | None = None
    min_score_for_idea: Decimal = Decimal("80")
    verbose: bool = False
    strategy_name: str | None = LIQUIDITY_GRAB_STRATEGY_NAME
    strategy_modes: tuple[LiquidityGrabMode, ...] = DEFAULT_STRATEGY_MODES
    enable_strategy_output: bool = True
    include_formatted_strategy_output: bool = True
    aggressive_toggle: bool = False
    htf_timeframe: str = "2d"
    bias_timeframe: str = "12h"
    execution_timeframe: str = "15m"
    confirmation_timeframe: str = "5m"

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

    @field_validator("interval", "htf_timeframe", "bias_timeframe", "execution_timeframe", "confirmation_timeframe")
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
    candle_count: int = 0
    current_price: MaybeDecimal = NA
    funding_rate: MaybeDecimal = NA
    open_interest: MaybeDecimal = NA
    candles_fetched: int = 0
    latest_close: MaybeDecimal = NA
    technical_score: MaybeInt = NA
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
    oi_direction: str = NA
    price_oi_relationship: str = NA
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
    technical_result: TechnicalStructureResult | None = None
    derivatives_result: DerivativesOrderflowResult | None = None
    risk_decision: RiskDecision | None = None
    score_result: OpportunityScoreResult | None = None
    trade_idea: TradeIdeaResult | None = None
    alert_result: AlertResult | None = None
    journal_entry: JournalEntryResult | None = None

    model_config = ConfigDict(frozen=True)


class ScannerRunResult(BaseModel):
    config: ScannerRunConfig
    results: tuple[ScannerSymbolResult, ...]
    scanned_symbols: int
    failed_symbols: int
    trade_ideas_created: int
    dry_run_alerts_created: int
    journal_entries_created: int

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
    open_interest: OpenInterestDTO | Any | None = None
    previous_open_interest: MaybeDecimal = NA
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
    selected_setup: LiquidityGrabSetup | None = None

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
        self.logger = log or logger

    async def run(self, config: ScannerRunConfig | Mapping[str, Any]) -> ScannerRunResult:
        run_config = config if isinstance(config, ScannerRunConfig) else ScannerRunConfig.model_validate(config)
        client, owns_client = self._exchange_client_for(run_config)
        results: list[ScannerSymbolResult] = []

        try:
            for symbol_config in run_config.symbols:
                try:
                    results.append(await self._scan_symbol(symbol_config, run_config, client))
                except Exception as exc:
                    self.logger.exception("Scanner failed for symbol=%s", symbol_config.symbol)
                    results.append(
                        ScannerSymbolResult(
                            symbol=symbol_config.symbol,
                            status=ScannerPipelineStatus.FAILED,
                            status_history=(ScannerPipelineStatus.FAILED,),
                            error_message=str(exc),
                            rejection_stage="scanner",
                            rejection_reasons=(str(exc),),
                        )
                    )
        finally:
            if owns_client and hasattr(client, "aclose"):
                await _maybe_await(client.aclose())

        return ScannerRunResult(
            config=run_config,
            results=tuple(results),
            scanned_symbols=len(results),
            failed_symbols=sum(1 for result in results if result.status == ScannerPipelineStatus.FAILED),
            trade_ideas_created=sum(1 for result in results if result.trade_idea is not None),
            dry_run_alerts_created=sum(
                1 for result in results if ScannerPipelineStatus.ALERT_DRY_RUN_CREATED in result.status_history
            ),
            journal_entries_created=sum(1 for result in results if result.journal_entry is not None),
        )

    def _exchange_client_for(self, config: ScannerRunConfig) -> tuple[BaseExchangeClient, bool]:
        if self.exchange_client is not None:
            return self.exchange_client, False
        if config.exchange == "binance":
            return BinanceFuturesClient(), True
        return BybitLinearClient(), True

    async def _scan_symbol(
        self,
        symbol_config: ScannerSymbolConfig,
        config: ScannerRunConfig,
        client: BaseExchangeClient,
    ) -> ScannerSymbolResult:
        symbol = symbol_config.symbol
        candles = await self._fetch_primary_candles(client, symbol, config)
        technical_candles = _technical_candles(candles)
        current_price = _current_price_from_candles(candles)
        optional_data = await self._fetch_optional_market_data(client, symbol)

        ticker_price = _decimal_field(optional_data.ticker, ("last_price", "mark_price"))
        if ticker_price != NA:
            current_price = ticker_price

        technical = self.technical_agent.analyze(technical_candles)
        base_missing = list(optional_data.missing_data)
        base_unverified = list(optional_data.unverified_data)
        strategy_execution = await self._run_strategy(
            client=client,
            symbol=symbol,
            config=config,
            primary_candles=candles,
            current_price=current_price,
            optional_data=optional_data,
            technical=technical,
        )
        base_missing.extend(strategy_execution.strategy_missing_data)
        base_unverified.extend(strategy_execution.strategy_unverified_data)

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
                technical_result=technical,
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
                    strategy_execution=strategy_execution,
                )
            candidate = candidate_result.candidate

        derivative_rejection = _derivatives_rejection(candidate.direction, derivatives)
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
                risk_decision=risk_decision,
                strategy_execution=strategy_execution,
            )

        strategy_catalyst_score = _strategy_catalyst_score(strategy_execution.selected_setup)
        score_result = self.scoring_engine.score(
            {
                "technical_score": Decimal(technical.structure_score),
                "derivatives_score": Decimal(derivatives.derivatives_score),
                "risk_approved": risk_decision.approved,
                "best_rr": _best_rr_for_scoring(risk_decision),
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
                risk_decision=risk_decision,
                score_result=score_result,
                strategy_execution=strategy_execution,
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
                "stop_loss": candidate.stop_loss,
                "take_profit_targets": candidate.take_profit_targets,
                "invalidation": candidate.invalidation,
                "opportunity_score": score_result.total_score,
                "opportunity_grade": score_result.grade,
                "opportunity_decision": score_result.decision,
                "risk_approved": risk_decision.approved,
                "best_rr": _best_rr_for_scoring(risk_decision),
                "technical_summary": candidate.technical_summary,
                "derivatives_summary": _derivatives_summary(derivatives),
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
                risk_decision=risk_decision,
                score_result=score_result,
                strategy_execution=strategy_execution,
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
            risk_decision=risk_decision,
            score_result=score_result,
            trade_idea=trade_idea,
            alert_result=alert_result,
            journal_entry=journal_entry,
            strategy_execution=strategy_execution,
        )

    async def _fetch_primary_candles(
        self,
        client: BaseExchangeClient,
        symbol: str,
        config: ScannerRunConfig,
    ) -> Sequence[Any]:
        primary_timeframe = config.interval.strip().lower()
        if config.exchange == "binance" and primary_timeframe == "2d":
            source_candles = await client.get_klines(symbol, SYNTHETIC_2D_SOURCE_TIMEFRAME, config.candle_limit * 2)
            return resample_ohlcv_candles(source_candles, target_interval="2d")
        return await client.get_klines(symbol, config.interval, config.candle_limit)

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
    ) -> _StrategyExecution:
        if not config.enable_strategy_output or config.strategy_name is None:
            return _StrategyExecution()

        candles_by_timeframe, timeframe_missing, timeframe_context = await self._fetch_strategy_timeframe_candles(
            client=client,
            symbol=symbol,
            config=config,
            primary_candles=primary_candles,
        )
        base_input = _liquidity_grab_input(
            symbol=symbol,
            candles_by_timeframe=candles_by_timeframe,
            current_price=current_price,
            optional_data=optional_data,
            technical=technical,
            aggressive_toggle=config.aggressive_toggle,
            htf_timeframe=config.htf_timeframe,
            bias_timeframe=config.bias_timeframe,
            execution_timeframe=config.execution_timeframe,
            confirmation_timeframe=config.confirmation_timeframe,
            timeframe_context=timeframe_context,
        )

        strategy_results: dict[str, LiquidityGrabResult] = {}
        diagnostics: dict[str, Any] = {}
        valid_modes: list[str] = []
        rejected_modes: list[str] = []
        missing_data = list(timeframe_missing)
        unverified_data: list[str] = []
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
            diagnostics[mode_name] = _strategy_diagnostics_for_setup(setup)
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
            selected_setup=selected_setup,
        )

    async def _fetch_strategy_timeframe_candles(
        self,
        *,
        client: BaseExchangeClient,
        symbol: str,
        config: ScannerRunConfig,
        primary_candles: Sequence[Any],
    ) -> tuple[dict[str, Sequence[Any]], tuple[str, ...], dict[str, Any]]:
        candles_by_timeframe: dict[str, Sequence[Any]] = {}
        missing_data: list[str] = []
        primary_timeframe = config.interval.strip().lower()
        htf_source: str = NA

        if config.htf_timeframe.strip().lower() == "2d":
            source_candles: Sequence[Any] = ()
            try:
                if primary_timeframe == SYNTHETIC_2D_SOURCE_TIMEFRAME:
                    source_candles = primary_candles
                else:
                    source_candles = await client.get_klines(
                        symbol,
                        SYNTHETIC_2D_SOURCE_TIMEFRAME,
                        config.candle_limit * 2,
                    )
                synthetic_2d = resample_ohlcv_candles(source_candles, target_interval="2d")
            except Exception as exc:
                self.logger.warning(
                    "Synthetic 2D candle creation failed for symbol=%s from 1d source: %s",
                    symbol,
                    exc,
                )
                synthetic_2d = []

            if synthetic_2d:
                candles_by_timeframe["2d"] = synthetic_2d
                htf_source = "synthetic_from_1d"
            else:
                missing_data.append("candles_2d: N/A")

        for timeframe in _direct_strategy_timeframes(config):
            if timeframe == primary_timeframe:
                candles_by_timeframe[timeframe] = primary_candles
                continue

            try:
                candles = await client.get_klines(symbol, timeframe, config.candle_limit)
            except Exception as exc:
                self.logger.warning("Optional strategy candles fetch failed for symbol=%s timeframe=%s: %s", symbol, timeframe, exc)
                missing_data.append(f"candles_{timeframe}: N/A")
                continue

            if candles:
                candles_by_timeframe[timeframe] = candles
            else:
                missing_data.append(f"candles_{timeframe}: N/A")

        return (
            candles_by_timeframe,
            _unique_strings(missing_data),
            {"htf_2d_context_source": htf_source},
        )

    async def _fetch_optional_market_data(self, client: BaseExchangeClient, symbol: str) -> _OptionalMarketData:
        missing_data: list[str] = []
        unverified_data: list[str] = []
        ticker = await self._optional_call(client, "get_ticker", symbol, missing_data, "ticker")
        funding = await self._optional_call(client, "get_funding_rate", symbol, missing_data, "funding_rate")
        open_interest = await self._optional_call(client, "get_open_interest", symbol, missing_data, "open_interest")
        previous_open_interest = _previous_open_interest_from(open_interest)

        if previous_open_interest == NA:
            history_value = await self._optional_previous_open_interest(client, symbol, missing_data)
            previous_open_interest = history_value

        if previous_open_interest == NA:
            missing_data.append("previous_open_interest: N/A")

        return _OptionalMarketData(
            ticker=ticker,
            funding=funding,
            open_interest=open_interest,
            previous_open_interest=previous_open_interest,
            missing_data=_unique_strings(missing_data),
            unverified_data=_unique_strings(unverified_data),
        )

    async def _optional_previous_open_interest(
        self,
        client: BaseExchangeClient,
        symbol: str,
        missing_data: list[str],
    ) -> MaybeDecimal:
        method = getattr(client, "get_open_interest_history", None)
        if not callable(method):
            return NA
        try:
            history = await _maybe_await(method(symbol=symbol, limit=2))
        except TypeError:
            history = await _maybe_await(method(symbol, 2))
        except Exception as exc:
            self.logger.warning("Optional previous OI fetch failed for symbol=%s: %s", symbol, exc)
            return NA

        if isinstance(history, Sequence) and not isinstance(history, (str, bytes)) and len(history) >= 2:
            previous = history[-2]
            return _decimal_field(previous, ("open_interest", "current_open_interest", "oi"))
        return NA

    async def _optional_call(
        self,
        client: BaseExchangeClient,
        method_name: str,
        symbol: str,
        missing_data: list[str],
        label: str,
    ) -> Any | None:
        method = getattr(client, method_name, None)
        if not callable(method):
            missing_data.append(f"{label}: N/A")
            return None
        try:
            return await _maybe_await(method(symbol))
        except Exception as exc:
            self.logger.warning("Optional %s fetch failed for symbol=%s: %s", label, symbol, exc)
            missing_data.append(f"{label}: N/A")
            return None

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
        risk_decision: RiskDecision | None = None,
        score_result: OpportunityScoreResult | None = None,
        trade_idea: TradeIdeaResult | None = None,
        alert_result: AlertResult | None = None,
        journal_entry: JournalEntryResult | None = None,
        strategy_execution: _StrategyExecution | None = None,
        rejection_stage_override: str | None = None,
    ) -> ScannerSymbolResult:
        cleaned_missing = _unique_strings(missing_data)
        cleaned_unverified = _unique_strings(unverified_data)
        strategy_execution = strategy_execution or _StrategyExecution()
        return ScannerSymbolResult(
            symbol=symbol,
            status=status,
            status_history=status_history,
            rejection_reason=rejection_reason,
            candle_count=len(candles),
            current_price=current_price,
            funding_rate=_decimal_field(optional_data.funding, ("funding_rate", "current_funding_rate")),
            open_interest=_decimal_field(optional_data.open_interest, ("open_interest", "current_open_interest", "oi")),
            candles_fetched=len(candles),
            latest_close=_current_price_from_candles(candles),
            technical_score=technical_result.structure_score if technical_result is not None else NA,
            derivatives_score=derivatives_result.derivatives_score if derivatives_result is not None else NA,
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
            oi_direction=derivatives_result.open_interest.direction if derivatives_result is not None else NA,
            price_oi_relationship=derivatives_result.price_oi_relationship.classification
            if derivatives_result is not None
            else NA,
            rejection_stage=rejection_stage_override or _rejection_stage_for(status),
            rejection_reasons=_rejection_reasons_for(status, rejection_reason),
            missing_data=cleaned_missing,
            unverified_data=cleaned_unverified,
            strategy_name=strategy_execution.strategy_name,
            strategy_results=strategy_execution.strategy_results,
            formatted_strategy_output=strategy_execution.formatted_strategy_output,
            strategy_diagnostics=strategy_execution.strategy_diagnostics,
            valid_strategy_modes=strategy_execution.valid_strategy_modes,
            rejected_strategy_modes=strategy_execution.rejected_strategy_modes,
            strategy_missing_data=strategy_execution.strategy_missing_data,
            strategy_unverified_data=strategy_execution.strategy_unverified_data,
            technical_result=technical_result,
            derivatives_result=derivatives_result,
            risk_decision=risk_decision,
            score_result=score_result,
            trade_idea=trade_idea,
            alert_result=alert_result,
            journal_entry=journal_entry,
        )


def _latest_swing_price(points: Sequence[Any]) -> MaybeDecimal:
    if not points:
        return NA
    return _quantize(points[-1].price)


def _rejection_stage_for(status: ScannerPipelineStatus) -> str:
    stages = {
        ScannerPipelineStatus.SCANNED_NO_SETUP: "technical",
        ScannerPipelineStatus.REJECTED_BY_TECHNICAL: "technical",
        ScannerPipelineStatus.REJECTED_BY_DERIVATIVES: "derivatives",
        ScannerPipelineStatus.REJECTED_BY_RISK: "risk",
        ScannerPipelineStatus.REJECTED_BY_SCORING: "scoring",
        ScannerPipelineStatus.FAILED: "scanner",
    }
    return stages.get(status, NA)


def _rejection_reasons_for(status: ScannerPipelineStatus, rejection_reason: str | None) -> tuple[str, ...]:
    if rejection_reason:
        return (rejection_reason,)
    if status in (
        ScannerPipelineStatus.IDEA_CREATED,
        ScannerPipelineStatus.ALERT_DRY_RUN_CREATED,
        ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
    ):
        return ()
    if status == ScannerPipelineStatus.SCANNED_NO_SETUP:
        return ("No deterministic setup context was detected.",)
    return ()


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
    htf_timeframe: str,
    bias_timeframe: str,
    execution_timeframe: str,
    confirmation_timeframe: str,
    timeframe_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    support_levels = _strategy_levels(technical.nearest_support, technical.recent_range_low)
    resistance_levels = _strategy_levels(technical.nearest_resistance, technical.recent_range_high)
    timeframe_context = timeframe_context or {}
    return {
        "symbol": symbol,
        "htf_timeframe": htf_timeframe,
        "bias_timeframe": bias_timeframe,
        "execution_timeframe": execution_timeframe,
        "confirmation_timeframe": confirmation_timeframe,
        "candles_2d": candles_by_timeframe.get("2d"),
        "candles_12h": candles_by_timeframe.get("12h"),
        "candles_4h": candles_by_timeframe.get("4h"),
        "candles_1h": candles_by_timeframe.get("1h"),
        "candles_15m": candles_by_timeframe.get("15m"),
        "candles_5m": candles_by_timeframe.get("5m"),
        "current_price": None if current_price == NA else current_price,
        "user_support_levels": support_levels or None,
        "user_resistance_levels": resistance_levels or None,
        "funding": optional_data.funding,
        "open_interest": optional_data.open_interest,
        "cvd": None,
        "liquidation_data": None,
        "aggressive_toggle": aggressive_toggle,
        "htf_2d_context_source": timeframe_context.get("htf_2d_context_source", NA),
    }


def _strategy_levels(*values: MaybeDecimal) -> tuple[Decimal, ...]:
    levels: list[Decimal] = []
    for value in values:
        if value != NA:
            levels.append(_quantize(value))
    return tuple(levels)


def _direct_strategy_timeframes(config: ScannerRunConfig) -> tuple[str, ...]:
    timeframes = (
        config.bias_timeframe,
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
        "is_valid": setup.is_valid,
        "status": setup.status,
        "bias": setup.bias,
        "timeframe": setup.timeframe,
        "htf_timeframe": setup.htf_timeframe,
        "bias_timeframe": setup.bias_timeframe,
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
        "confirmation_structure_shift_status": setup.confirmation_structure_shift_status,
        "confirmation_bos_choch_reason": setup.confirmation_bos_choch_reason,
        "first_failed_gate": setup.first_failed_gate,
        "trust_grade": setup.trust_meter.grade,
        "trust_percentage": setup.trust_meter.percentage,
        "gates_passed": setup.gates_passed,
        "gates_failed": setup.gates_failed,
        "hard_rejection_reasons": setup.hard_rejection_reasons,
        "sweep_diagnostics": setup.sweep_diagnostics,
        "bos_choch_diagnostics": setup.structure_shift_diagnostics,
        "ob_fvg_diagnostics": setup.ob_fvg_diagnostics,
        "fib_diagnostics": setup.fib_diagnostics,
        "rr_diagnostics": setup.rr_diagnostics,
        "trust_meter_diagnostics": setup.trust_meter_diagnostics,
        "strategy_diagnostics": setup.strategy_diagnostics,
        "missing_data": setup.missing_data,
        "unverified_data": setup.unverified_data,
    }


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


def _derivatives_rejection(direction: Literal["long", "short"], result: DerivativesOrderflowResult) -> str | None:
    if result.data_quality.status == "invalid":
        return result.data_quality.reason
    if Decimal(result.derivatives_score) < MIN_DERIVATIVES_SCORE:
        return f"Derivatives score {result.derivatives_score} is below 40."
    if direction == "long" and result.risk_flags.crowded_long_risk:
        return "Crowded long derivatives risk is active."
    if direction == "short" and result.risk_flags.crowded_short_risk:
        return "Crowded short derivatives risk is active."
    if result.risk_flags.conflicting_context:
        return "Derivatives context conflicts with price direction."
    return None


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


def _best_rr_for_scoring(result: RiskDecision) -> Decimal:
    if result.best_risk_reward_ratio == NA:
        return Decimal("0")
    return result.best_risk_reward_ratio


def _derivatives_summary(result: DerivativesOrderflowResult) -> str:
    relationship = result.price_oi_relationship.classification
    if relationship == NA:
        relationship = "price/OI relationship N/A"
    flags = ", ".join(result.active_risk_flags) if result.active_risk_flags else "no active derivatives risk flags"
    return f"{relationship}; derivatives score {result.derivatives_score}; {flags}."


def _missing_data_from_derivatives(result: DerivativesOrderflowResult) -> tuple[str, ...]:
    return tuple(f"{field}: N/A" for field in result.data_quality.missing_fields)


def _unverified_data_from_derivatives(result: DerivativesOrderflowResult) -> tuple[str, ...]:
    values = [f"{field}: Unverified" for field in result.data_quality.unverified_fields]
    if result.data_quality.reliability == "Unverified":
        values.append("derivatives: Unverified")
    return tuple(values)


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
    "ScannerPipelineStatus",
    "ScannerRiskConfig",
    "ScannerRunConfig",
    "ScannerRunResult",
    "ScannerRunner",
    "ScannerSymbolConfig",
    "ScannerSymbolResult",
]
