from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.data.dtos import NA, MaybeDecimal

OUTPUT_QUANT = Decimal("0.00000001")
DEFAULT_MAX_SELECTED_SETUPS = 3
DEFAULT_MAX_PORTFOLIO_RISK_PCT = Decimal("3")
DEFAULT_MAX_BETA_GROUP_RISK_PCT = Decimal("1.5")
MODE_ORDER = ("challenge", "swing", "scalp")


class PortfolioDecision(str, Enum):
    SELECTED = "SELECTED"
    WATCHLIST_ONLY = "WATCHLIST_ONLY"
    REJECTED_LOWER_QUALITY_DUPLICATE = "REJECTED_LOWER_QUALITY_DUPLICATE"
    REJECTED_PORTFOLIO_RISK_LIMIT = "REJECTED_PORTFOLIO_RISK_LIMIT"
    REJECTED_CORRELATED_EXPOSURE = "REJECTED_CORRELATED_EXPOSURE"
    REJECTED_NO_VALID_TRADE = "REJECTED_NO_VALID_TRADE"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"


class BetaGroup(str, Enum):
    BTC_MAJOR = "BTC_MAJOR"
    ETH_BETA = "ETH_BETA"
    SOL_BETA = "SOL_BETA"
    L1_L2 = "L1_L2"
    MEME = "MEME"
    AI = "AI"
    RWA = "RWA"
    DEFI = "DEFI"
    UNKNOWN = "UNKNOWN"


class PortfolioRiskLimits(BaseModel):
    max_selected_setups: int = DEFAULT_MAX_SELECTED_SETUPS
    max_portfolio_risk_pct: Decimal = DEFAULT_MAX_PORTFOLIO_RISK_PCT
    max_beta_group_risk_pct: Decimal = DEFAULT_MAX_BETA_GROUP_RISK_PCT
    allow_correlated_setups: bool = False

    model_config = ConfigDict(frozen=True)

    @field_validator("max_selected_setups")
    @classmethod
    def _max_selected_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_selected_setups must be at least 1")
        return value

    @field_validator("max_portfolio_risk_pct", "max_beta_group_risk_pct", mode="before")
    @classmethod
    def _normalize_decimal(cls, value: Any) -> Decimal:
        decimal = _decimal_from(value, "portfolio risk limit")
        if decimal < 0:
            raise ValueError("risk limits must be zero or greater")
        return _quantize(decimal)


class PortfolioCandidate(BaseModel):
    symbol: str
    mode: str = NA
    direction: Literal["long", "short", "N/A"] = NA
    quality_state: str = NA
    quality_score: int = Field(default=0, ge=0, le=100)
    tradeability_score: int = Field(default=0, ge=0, le=100)
    edge_score: MaybeDecimal = NA
    expectancy: MaybeDecimal = NA
    rr: MaybeDecimal = NA
    risk_pct: Decimal = Decimal("0")
    sector: str = NA
    narrative: str = NA
    beta_group: BetaGroup = BetaGroup.UNKNOWN
    derivatives_score: int = Field(default=0, ge=0, le=100)
    execution_risk_score: int = Field(default=100, ge=0, le=100)
    derivatives_clean: bool | Literal["N/A"] = NA
    memory_confidence: str = NA
    memory_preference_adjustment: int = Field(default=0, ge=-5, le=5)
    regime_compatibility_score: int = Field(default=50, ge=0, le=100)
    regime_confidence_adjustment: int = Field(default=0, ge=-20, le=10)
    valid_trade: bool = False
    near_miss: bool = False
    data_incomplete: bool = False
    regime_blocked: bool = False
    regime_warnings: tuple[str, ...] = ()
    decision: PortfolioDecision = PortfolioDecision.REJECTED_NO_VALID_TRADE
    decision_reason: str = NA

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator("mode", "quality_state", "sector", "narrative", "memory_confidence", "decision_reason", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        text = _display(value)
        return text if text else NA

    @field_validator("regime_warnings", mode="before")
    @classmethod
    def _normalize_warnings(cls, value: Any) -> tuple[str, ...]:
        return _sequence_values(value)

    @field_validator("edge_score", "expectancy", "rr", mode="before")
    @classmethod
    def _normalize_maybe_decimal(cls, value: Any) -> MaybeDecimal:
        if _is_missing(value):
            return NA
        return _quantize(_decimal_from(value, "portfolio candidate"))

    @field_validator("risk_pct", mode="before")
    @classmethod
    def _normalize_risk_pct(cls, value: Any) -> Decimal:
        if _is_missing(value):
            return Decimal("0")
        decimal = _decimal_from(value, "portfolio candidate risk")
        if decimal < 0:
            raise ValueError("risk_pct must be zero or greater")
        return _quantize(decimal)

    @field_validator("beta_group", mode="before")
    @classmethod
    def _normalize_beta_group(cls, value: Any) -> BetaGroup:
        text = _display(value).upper()
        return BetaGroup.__members__.get(text, BetaGroup.UNKNOWN)

    @field_validator("direction", mode="before")
    @classmethod
    def _normalize_direction(cls, value: Any) -> str:
        text = _display(value).lower()
        if text in ("long", "bullish"):
            return "long"
        if text in ("short", "bearish"):
            return "short"
        return NA

    @model_validator(mode="after")
    def _derive_flags(self) -> PortfolioCandidate:
        quality = self.quality_state.upper()
        data_incomplete = self.data_incomplete or quality == "DATA_ISSUE"
        near_miss = self.near_miss or quality == "WATCHLIST_NEAR_MISS"
        valid_trade = (
            self.valid_trade
            or (
                quality in {"HIGH_QUALITY_TRADE", "VALID_BUT_LOWER_QUALITY"}
                and self.mode != NA
                and self.direction in ("long", "short")
                and self.rr != NA
                and self.risk_pct > 0
                and not data_incomplete
            )
        )
        object.__setattr__(self, "valid_trade", valid_trade)
        object.__setattr__(self, "near_miss", near_miss)
        object.__setattr__(self, "data_incomplete", data_incomplete)
        return self


class PortfolioExposureGroup(BaseModel):
    beta_group: BetaGroup
    risk_pct: Decimal
    selected_count: int
    symbols: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)

    @field_validator("risk_pct", mode="before")
    @classmethod
    def _normalize_risk_pct(cls, value: Any) -> Decimal:
        return _quantize(_decimal_from(value, "portfolio exposure risk"))


class PortfolioSelectionInput(BaseModel):
    candidates: tuple[PortfolioCandidate, ...]
    risk_limits: PortfolioRiskLimits = Field(default_factory=PortfolioRiskLimits)

    model_config = ConfigDict(frozen=True)

    @field_validator("candidates", mode="before")
    @classmethod
    def _normalize_candidates(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if isinstance(value, Mapping):
            return (value,)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return tuple(value)
        return (value,)


class PortfolioSelectionResult(BaseModel):
    risk_limits: PortfolioRiskLimits
    selected_candidates: tuple[PortfolioCandidate, ...]
    rejected_candidates: tuple[PortfolioCandidate, ...]
    exposure_summary: tuple[PortfolioExposureGroup, ...]
    portfolio_warnings: tuple[str, ...] = ()
    selected_count: int = 0
    total_risk_pct: Decimal = Decimal("0")
    rejected_due_to_correlation: int = 0
    rejected_due_to_risk_limit: int = 0

    model_config = ConfigDict(frozen=True)


def select_portfolio(
    selection_input: PortfolioSelectionInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> PortfolioSelectionResult:
    data = _normalize_selection_input(selection_input, overrides)
    risk_limits = data.risk_limits
    decisions: list[PortfolioCandidate] = []
    selected: list[PortfolioCandidate] = []
    group_risk: dict[BetaGroup, Decimal] = {}

    for candidate in sorted(data.candidates, key=_candidate_sort_key):
        preselection_decision = _preselection_decision(candidate)
        if preselection_decision is not None:
            decisions.append(_with_decision(candidate, *preselection_decision))
            continue

        same_group_selected = [
            item
            for item in selected
            if item.beta_group == candidate.beta_group and candidate.beta_group != BetaGroup.UNKNOWN
        ]
        if same_group_selected and not risk_limits.allow_correlated_setups:
            existing = same_group_selected[0]
            decision = (
                PortfolioDecision.REJECTED_LOWER_QUALITY_DUPLICATE
                if _quality_tuple(candidate) <= _quality_tuple(existing)
                else PortfolioDecision.REJECTED_CORRELATED_EXPOSURE
            )
            decisions.append(
                _with_decision(
                    candidate,
                    decision,
                    f"{candidate.beta_group.value} exposure already selected via {existing.symbol}.",
                )
            )
            continue

        candidate_risk = _quantize(candidate.risk_pct)
        current_total_risk = _selected_risk(selected)
        current_group_risk = group_risk.get(candidate.beta_group, Decimal("0"))
        if len(selected) >= risk_limits.max_selected_setups:
            decisions.append(
                _with_decision(
                    candidate,
                    PortfolioDecision.REJECTED_PORTFOLIO_RISK_LIMIT,
                    f"Max selected setups limit {risk_limits.max_selected_setups} is already reached.",
                )
            )
            continue
        if current_total_risk + candidate_risk > risk_limits.max_portfolio_risk_pct:
            decisions.append(
                _with_decision(
                    candidate,
                    PortfolioDecision.REJECTED_PORTFOLIO_RISK_LIMIT,
                    (
                        f"Portfolio risk would exceed {risk_limits.max_portfolio_risk_pct}% "
                        f"with {candidate.symbol}."
                    ),
                )
            )
            continue
        if current_group_risk + candidate_risk > risk_limits.max_beta_group_risk_pct:
            decisions.append(
                _with_decision(
                    candidate,
                    PortfolioDecision.REJECTED_PORTFOLIO_RISK_LIMIT,
                    (
                        f"{candidate.beta_group.value} risk would exceed "
                        f"{risk_limits.max_beta_group_risk_pct}%."
                    ),
                )
            )
            continue

        selected_candidate = _with_decision(candidate, PortfolioDecision.SELECTED, "Selected by portfolio rules.")
        selected.append(selected_candidate)
        decisions.append(selected_candidate)
        group_risk[candidate.beta_group] = current_group_risk + candidate_risk

    rejected = tuple(candidate for candidate in decisions if candidate.decision != PortfolioDecision.SELECTED)
    exposure_summary = _exposure_summary(selected)
    warnings = _portfolio_warnings(selected, rejected)
    rejected_due_to_correlation = sum(
        1
        for candidate in rejected
        if candidate.decision
        in (
            PortfolioDecision.REJECTED_LOWER_QUALITY_DUPLICATE,
            PortfolioDecision.REJECTED_CORRELATED_EXPOSURE,
        )
    )
    rejected_due_to_risk_limit = sum(
        1 for candidate in rejected if candidate.decision == PortfolioDecision.REJECTED_PORTFOLIO_RISK_LIMIT
    )
    return PortfolioSelectionResult(
        risk_limits=risk_limits,
        selected_candidates=tuple(selected),
        rejected_candidates=rejected,
        exposure_summary=exposure_summary,
        portfolio_warnings=warnings,
        selected_count=len(selected),
        total_risk_pct=_selected_risk(selected),
        rejected_due_to_correlation=rejected_due_to_correlation,
        rejected_due_to_risk_limit=rejected_due_to_risk_limit,
    )


def build_portfolio_selection_from_scan(
    scan_result: Any,
    *,
    risk_limits: PortfolioRiskLimits | Mapping[str, Any] | None = None,
) -> PortfolioSelectionResult:
    limits = (
        risk_limits
        if isinstance(risk_limits, PortfolioRiskLimits)
        else PortfolioRiskLimits.model_validate(risk_limits or {})
    )
    config = getattr(scan_result, "config", None)
    default_risk_pct = _decimal_or_na(getattr(config, "risk_per_trade_pct", NA))
    adjustment = _regime_adjustment(scan_result)
    candidates = tuple(
        _apply_regime_to_candidate(
            _candidate_from_symbol_result(symbol_result, default_risk_pct=default_risk_pct),
            adjustment,
        )
        for symbol_result in getattr(scan_result, "results", ())
    )
    return select_portfolio(PortfolioSelectionInput(candidates=candidates, risk_limits=limits))


def selected_symbols(selection: PortfolioSelectionResult | None) -> tuple[str, ...]:
    if selection is None:
        return ()
    return tuple(candidate.symbol for candidate in selection.selected_candidates)


def format_portfolio_selection_summary(selection: PortfolioSelectionResult) -> str:
    return "\n".join(
        (
            "Portfolio Selection",
            f"- Selected setups: {selection.selected_count}",
            f"- Total risk: {_pct_text(selection.total_risk_pct)}%",
            f"- Exposure groups: {_exposure_summary_text(selection.exposure_summary)}",
            f"- Rejected due to correlation: {selection.rejected_due_to_correlation}",
            f"- Rejected due to risk limit: {selection.rejected_due_to_risk_limit}",
        )
    )


def _normalize_selection_input(
    selection_input: PortfolioSelectionInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> PortfolioSelectionInput:
    if selection_input is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(selection_input, PortfolioSelectionInput):
        raw = selection_input.model_dump()
        raw.update(overrides)
    else:
        raw = dict(selection_input)
        raw.update(overrides)
    return PortfolioSelectionInput.model_validate(raw)


def _candidate_from_symbol_result(symbol_result: Any, *, default_risk_pct: MaybeDecimal) -> PortfolioCandidate:
    mode = _mode_from_symbol_result(symbol_result)
    diagnostics = _diagnostics_for_mode(symbol_result, mode)
    quality = getattr(symbol_result, "setup_quality", None)
    trade_idea = getattr(symbol_result, "trade_idea", None)
    risk_decision = getattr(symbol_result, "risk_decision", None)
    quality_state = _display(_attr(quality, "quality_state"))
    quality_score = _int_score(_attr(quality, "quality_score"))
    tradeability_score = _int_score(_attr(quality, "tradeability_score"))
    execution_risk_score = _int_score(_attr(quality, "execution_risk_score"), default=100)
    edge_score = _maybe_decimal(_attr(quality, "profitability_edge_score"))
    expectancy = _expectancy(symbol_result)
    rr = _best_rr(symbol_result, diagnostics)
    risk_pct = _risk_pct(symbol_result, default_risk_pct)
    sector = _first_non_na(diagnostics.get("sector"), _attr(symbol_result, "sector"))
    narrative = _first_non_na(diagnostics.get("narrative"), diagnostics.get("theme"), _attr(symbol_result, "narrative"))
    beta_group = _beta_group(
        symbol=_display(getattr(symbol_result, "symbol", NA)),
        supplied=_first_non_na(diagnostics.get("beta_group"), diagnostics.get("exposure_group")),
        sector=sector,
        narrative=narrative,
    )
    direction = _direction(symbol_result, diagnostics, trade_idea)
    data_incomplete = quality_state == "DATA_ISSUE" or _is_data_incomplete(symbol_result, diagnostics)
    near_miss = quality_state == "WATCHLIST_NEAR_MISS"
    valid_trade = _valid_trade(symbol_result, quality_state, mode, direction, rr, risk_pct, data_incomplete)
    return PortfolioCandidate(
        symbol=_display(getattr(symbol_result, "symbol", NA)),
        mode=mode,
        direction=direction,
        quality_state=quality_state,
        quality_score=quality_score,
        tradeability_score=tradeability_score,
        edge_score=edge_score,
        expectancy=expectancy,
        rr=rr,
        risk_pct=risk_pct,
        sector=sector,
        narrative=narrative,
        beta_group=beta_group,
        derivatives_score=_int_score(getattr(symbol_result, "derivatives_score", 0)),
        execution_risk_score=execution_risk_score,
        derivatives_clean=_derivatives_clean(symbol_result, diagnostics),
        memory_confidence=_memory_confidence(symbol_result),
        memory_preference_adjustment=_memory_preference_adjustment(symbol_result),
        regime_compatibility_score=_int_score(getattr(symbol_result, "regime_compatibility_score", 50), default=50),
        # Only use current-run regime diagnostic adjustment; never fall back to historical regime_penalty
        regime_confidence_adjustment=_int_score(
            (getattr(symbol_result, "regime_diagnostics", {}) or {}).get("portfolio_confidence_adjustment", 0)
            if isinstance(getattr(symbol_result, "regime_diagnostics", {}), Mapping)
            else 0,
            default=0,
            lower=-20,
            upper=10,
        ),
        valid_trade=valid_trade,
        near_miss=near_miss,
        data_incomplete=data_incomplete,
    )


def _preselection_decision(candidate: PortfolioCandidate) -> tuple[PortfolioDecision, str] | None:
    if candidate.regime_blocked:
        reason = candidate.regime_warnings[0] if candidate.regime_warnings else "Market climate blocks this setup mode."
        return PortfolioDecision.WATCHLIST_ONLY, reason
    if candidate.data_incomplete:
        return PortfolioDecision.DATA_INCOMPLETE, "Required setup or risk data is incomplete."
    if candidate.near_miss:
        return PortfolioDecision.WATCHLIST_ONLY, "Near-miss remains watchlist only."
    if not candidate.valid_trade:
        return PortfolioDecision.REJECTED_NO_VALID_TRADE, "Candidate is not a valid trade setup."
    return None


def _with_decision(
    candidate: PortfolioCandidate,
    decision: PortfolioDecision,
    reason: str,
) -> PortfolioCandidate:
    return candidate.model_copy(update={"decision": decision, "decision_reason": reason})


def _candidate_sort_key(candidate: PortfolioCandidate) -> tuple[Any, ...]:
    return (
        0 if candidate.valid_trade else 1,
        -candidate.quality_score,
        -_ranking_edge(candidate),
        -candidate.memory_preference_adjustment,
        -candidate.regime_confidence_adjustment,
        -candidate.regime_compatibility_score,
        -_decimal_score(candidate.rr),
        -candidate.tradeability_score,
        -_derivatives_cleanliness_score(candidate),
        candidate.execution_risk_score,
        candidate.symbol,
    )


def _quality_tuple(candidate: PortfolioCandidate) -> tuple[Any, ...]:
    return (
        candidate.quality_score,
        _ranking_edge(candidate),
        candidate.memory_preference_adjustment,
        candidate.regime_confidence_adjustment,
        candidate.regime_compatibility_score,
        _decimal_score(candidate.rr),
        candidate.tradeability_score,
        _derivatives_cleanliness_score(candidate),
        -candidate.execution_risk_score,
    )


def _ranking_edge(candidate: PortfolioCandidate) -> Decimal:
    expectancy = _decimal_score(candidate.expectancy)
    if expectancy != Decimal("0"):
        return expectancy
    return _decimal_score(candidate.edge_score)


def _derivatives_cleanliness_score(candidate: PortfolioCandidate) -> int:
    score = candidate.derivatives_score
    if candidate.derivatives_clean is True:
        score += 20
    elif candidate.derivatives_clean is False:
        score -= 30
    return score


def _selected_risk(selected: Sequence[PortfolioCandidate]) -> Decimal:
    return _quantize(sum((candidate.risk_pct for candidate in selected), Decimal("0")))


def _exposure_summary(selected: Sequence[PortfolioCandidate]) -> tuple[PortfolioExposureGroup, ...]:
    grouped: dict[BetaGroup, list[PortfolioCandidate]] = {}
    for candidate in selected:
        grouped.setdefault(candidate.beta_group, []).append(candidate)
    return tuple(
        PortfolioExposureGroup(
            beta_group=beta_group,
            risk_pct=_selected_risk(candidates),
            selected_count=len(candidates),
            symbols=tuple(candidate.symbol for candidate in candidates),
        )
        for beta_group, candidates in sorted(grouped.items(), key=lambda item: item[0].value)
    )


def _portfolio_warnings(
    selected: Sequence[PortfolioCandidate],
    rejected: Sequence[PortfolioCandidate],
) -> tuple[str, ...]:
    warnings: list[str] = []
    if not selected:
        warnings.append("No valid setup survived portfolio selection.")
    if any(candidate.decision == PortfolioDecision.DATA_INCOMPLETE for candidate in rejected):
        warnings.append("Some candidates had incomplete data and were not selected.")
    regime_warnings = _unique_strings(
        tuple(warning for candidate in (*selected, *rejected) for warning in candidate.regime_warnings)
    )
    warnings.extend(regime_warnings)
    if any(
        candidate.decision
        in (
            PortfolioDecision.REJECTED_LOWER_QUALITY_DUPLICATE,
            PortfolioDecision.REJECTED_CORRELATED_EXPOSURE,
        )
        for candidate in rejected
    ):
        warnings.append("Correlated beta exposure was reduced by keeping the strongest setup per group.")
    return tuple(warnings)


def _regime_adjustment(scan_result: Any) -> Any | None:
    market_regime = getattr(scan_result, "market_regime", None)
    if market_regime is None or getattr(market_regime, "enabled", True) is False:
        return None
    return getattr(market_regime, "adjustment", None)


def _apply_regime_to_candidate(candidate: PortfolioCandidate, adjustment: Any | None) -> PortfolioCandidate:
    if adjustment is None:
        return candidate
    warnings = list(candidate.regime_warnings)
    updates: dict[str, Any] = {
        "regime_confidence_adjustment": int(getattr(adjustment, "portfolio_confidence_adjustment", 0) or 0),
    }
    mode_allowed = _mode_allowed_by_regime(candidate.mode, adjustment)
    if candidate.valid_trade and not mode_allowed:
        warnings.append(f"Market climate blocks {candidate.mode} setups: {_display(getattr(adjustment, 'explanation', NA))}")
        updates.update(
            {
                "valid_trade": False,
                "near_miss": True,
                "regime_blocked": True,
                "regime_warnings": _unique_strings(warnings),
            }
        )
        return candidate.model_copy(update=updates)
    risk_multiplier = _regime_risk_multiplier(adjustment)
    if candidate.valid_trade and risk_multiplier < Decimal("1"):
        warnings.append(
            f"Market climate risk multiplier {risk_multiplier} applies: {_display(getattr(adjustment, 'explanation', NA))}"
        )
        updates.update(
            {
                "risk_pct": _quantize(candidate.risk_pct * risk_multiplier),
                "regime_warnings": _unique_strings(warnings),
            }
        )
        return candidate.model_copy(update=updates)
    if updates["regime_confidence_adjustment"] != 0:
        return candidate.model_copy(update=updates)
    return candidate


def _regime_risk_multiplier(adjustment: Any | None) -> Decimal:
    if adjustment is None:
        return Decimal("1")
    value = _maybe_decimal(getattr(adjustment, "risk_multiplier", Decimal("1")))
    if value == NA:
        return Decimal("1")
    return min(Decimal("1"), max(Decimal("0"), value))


def _mode_allowed_by_regime(mode: str, adjustment: Any) -> bool:
    normalized = _display(mode).lower()
    if normalized == "challenge":
        return bool(getattr(adjustment, "allow_challenge", True))
    if normalized == "swing":
        return bool(getattr(adjustment, "allow_swings", True))
    if normalized == "scalp":
        return bool(getattr(adjustment, "allow_scalps", True))
    return True


def _mode_from_symbol_result(symbol_result: Any) -> str:
    valid_modes = tuple(_display(mode).lower() for mode in getattr(symbol_result, "valid_strategy_modes", ()) if mode)
    for mode in MODE_ORDER:
        if mode in valid_modes:
            return mode
    if valid_modes:
        return valid_modes[0]

    diagnostics = getattr(symbol_result, "strategy_diagnostics", {}) or {}
    if isinstance(diagnostics, Mapping):
        for mode in MODE_ORDER:
            item = diagnostics.get(mode)
            if isinstance(item, Mapping) and item.get("is_valid") is True:
                return mode
        if diagnostics:
            return _display(next(iter(diagnostics.keys()))).lower()
    rejected_modes = tuple(_display(mode).lower() for mode in getattr(symbol_result, "rejected_strategy_modes", ()) if mode)
    return rejected_modes[0] if rejected_modes else NA


def _diagnostics_for_mode(symbol_result: Any, mode: str) -> Mapping[str, Any]:
    diagnostics = getattr(symbol_result, "strategy_diagnostics", {}) or {}
    if not isinstance(diagnostics, Mapping):
        return {}
    if mode in diagnostics and isinstance(diagnostics[mode], Mapping):
        return diagnostics[mode]
    for item in diagnostics.values():
        if isinstance(item, Mapping):
            return item
    return {}


def _direction(symbol_result: Any, diagnostics: Mapping[str, Any], trade_idea: Any | None) -> str:
    for value in (
        _attr(trade_idea, "direction"),
        diagnostics.get("direction"),
        diagnostics.get("bias"),
        diagnostics.get("setup_direction"),
    ):
        normalized = _direction_text(value)
        if normalized != NA:
            return normalized
    for value in (diagnostics.get("sweep_diagnostics"), diagnostics.get("bos_choch_diagnostics")):
        text = _display(value).lower()
        if "bullish" in text:
            return "long"
        if "bearish" in text:
            return "short"
    return NA


def _valid_trade(
    symbol_result: Any,
    quality_state: str,
    mode: str,
    direction: str,
    rr: MaybeDecimal,
    risk_pct: Decimal,
    data_incomplete: bool,
) -> bool:
    if data_incomplete:
        return False
    valid_quality = quality_state in {"HIGH_QUALITY_TRADE", "VALID_BUT_LOWER_QUALITY"}
    valid_mode = mode != NA and bool(getattr(symbol_result, "valid_strategy_modes", ()))
    return valid_quality and valid_mode and direction in ("long", "short") and rr != NA and risk_pct > 0


def _is_data_incomplete(symbol_result: Any, diagnostics: Mapping[str, Any]) -> bool:
    if getattr(symbol_result, "error_message", None):
        return True
    missing = (
        *_sequence_values(getattr(symbol_result, "missing_data", ())),
        *_sequence_values(getattr(symbol_result, "strategy_missing_data", ())),
        *_sequence_values(diagnostics.get("missing_data")),
    )
    critical_prefixes = (
        "candles:",
        "candles_15m:",
        "candles_5m:",
        "execution_candles:",
        "confirmation_candles:",
        "current_price:",
    )
    return any(item.startswith(critical_prefixes) for item in missing)


def _derivatives_clean(symbol_result: Any, diagnostics: Mapping[str, Any]) -> bool | Literal["N/A"]:
    conflict = _first_non_na(diagnostics.get("derivatives_conflict_reason"), getattr(symbol_result, "crowding_risk", NA))
    if _display(diagnostics.get("derivatives_supports_trade")) == "False":
        return False
    if _display(conflict) not in (NA, "low", "balanced", "normal"):
        return False
    if _sequence_values(getattr(symbol_result, "derivatives_warnings", ())):
        return False
    if _display(diagnostics.get("derivatives_supports_trade")) == "True":
        return True
    return NA


def _best_rr(symbol_result: Any, diagnostics: Mapping[str, Any]) -> MaybeDecimal:
    risk_decision = getattr(symbol_result, "risk_decision", None)
    trade_idea = getattr(symbol_result, "trade_idea", None)
    values = (
        _attr(trade_idea, "best_rr"),
        _attr(risk_decision, "best_risk_reward_ratio"),
        diagnostics.get("rr_to_tp2"),
        diagnostics.get("best_rr"),
    )
    decimals = [_maybe_decimal(value) for value in values]
    numeric = [value for value in decimals if value != NA]
    if not numeric:
        return NA
    return max(numeric)


def _risk_pct(symbol_result: Any, default_risk_pct: MaybeDecimal) -> Decimal:
    risk_decision = getattr(symbol_result, "risk_decision", None)
    config = getattr(symbol_result, "config", None)
    risk_amount = _attr(_attr(risk_decision, "position_sizing"), "risk_amount")
    account_equity = _attr(config, "account_equity")
    if _is_missing(account_equity):
        account_equity = NA
    if risk_amount != NA and account_equity != NA:
        try:
            equity = _decimal_from(account_equity, "account equity")
            if equity > 0:
                return _quantize(_decimal_from(risk_amount, "risk amount") / equity * Decimal("100"))
        except ValueError:
            pass
    if default_risk_pct != NA:
        return _quantize(default_risk_pct)
    return Decimal("0")


def _expectancy(symbol_result: Any) -> MaybeDecimal:
    metrics = getattr(symbol_result, "expectancy_metrics", {}) or {}
    if isinstance(metrics, Mapping):
        for key in ("expectancy", "expectancy_r", "average_r"):
            value = _maybe_decimal(metrics.get(key))
            if value != NA:
                return value
    summary = getattr(symbol_result, "historical_match_summary", {}) or {}
    if isinstance(summary, Mapping) and isinstance(summary.get("expectancy_metrics"), Mapping):
        for key in ("expectancy", "expectancy_r", "average_r"):
            value = _maybe_decimal(summary["expectancy_metrics"].get(key))
            if value != NA:
                return value
    return NA


def _memory_confidence(symbol_result: Any) -> str:
    memory = getattr(symbol_result, "performance_memory", {}) or {}
    if isinstance(memory, Mapping):
        confidence = _display(memory.get("confidence_bucket"))
        if confidence != NA:
            return confidence
    return _display(getattr(symbol_result, "confidence_bucket", NA))


def _memory_preference_adjustment(symbol_result: Any) -> int:
    memory = getattr(symbol_result, "performance_memory", {}) or {}
    adjustments: Any = {}
    if isinstance(memory, Mapping):
        adjustments = memory.get("memory_adjustments") or {}
    if not isinstance(adjustments, Mapping):
        adjustments = getattr(symbol_result, "memory_adjustments", {}) or {}
    if not isinstance(adjustments, Mapping):
        return 0
    value = _maybe_decimal(adjustments.get("portfolio_preference_adjustment"))
    if value == NA:
        return 0
    return int(max(Decimal("-5"), min(Decimal("5"), value)).to_integral_value(rounding="ROUND_HALF_UP"))


def _beta_group(symbol: str, supplied: str, sector: str, narrative: str) -> BetaGroup:
    supplied_group = BetaGroup.__members__.get(_display(supplied).upper())
    if supplied_group is not None:
        return supplied_group
    text = " ".join((_base_asset(symbol), sector, narrative)).upper()
    tokens = set(text.replace("-", "_").replace("/", "_").split())
    base = _base_asset(symbol).upper()
    if base in {"BTC", "WBTC"}:
        return BetaGroup.BTC_MAJOR
    if base in {"ETH", "ETC", "LDO", "ENS"}:
        return BetaGroup.ETH_BETA
    if base in {"SOL", "JUP", "JTO", "PYTH", "RAY"}:
        return BetaGroup.SOL_BETA
    if base in {"DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "MEME", "BOME", "PNUT", "TRUMP"}:
        return BetaGroup.MEME
    if base in {"TAO", "FET", "RNDR", "RENDER", "AIXBT", "VIRTUAL", "GRASS", "AI"} or "AI" in tokens:
        return BetaGroup.AI
    if base in {"ONDO", "PENDLE", "OM", "POLYX", "CFG"} or "RWA" in tokens:
        return BetaGroup.RWA
    if base in {"UNI", "AAVE", "CRV", "MKR", "COMP", "SNX", "DYDX", "GMX", "CAKE", "RUNE", "ENA"}:
        return BetaGroup.DEFI
    if base in {"ADA", "AVAX", "DOT", "NEAR", "ATOM", "SUI", "APT", "SEI", "INJ", "TON", "MATIC", "POL", "OP", "ARB"}:
        return BetaGroup.L1_L2
    if tokens & {"MEME", "MEMECOIN", "MEMES"}:
        return BetaGroup.MEME
    if tokens & {"DEFI", "DEX"}:
        return BetaGroup.DEFI
    if tokens & {"L1", "L2", "LAYER1", "LAYER2"}:
        return BetaGroup.L1_L2
    return BetaGroup.UNKNOWN


def _base_asset(symbol: str) -> str:
    text = _display(symbol).upper()
    for prefix in ("1000000", "10000", "1000"):
        if text.startswith(prefix):
            text = text.removeprefix(prefix)
    for suffix in ("USDT", "USDC", "USD", "PERP"):
        if text.endswith(suffix):
            return text.removesuffix(suffix)
    return text


def _exposure_summary_text(exposures: Sequence[PortfolioExposureGroup]) -> str:
    if not exposures:
        return NA
    return ", ".join(f"{item.beta_group.value} {_pct_text(item.risk_pct)}%" for item in exposures)


def _pct_text(value: Decimal) -> str:
    text = format(_quantize(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _direction_text(value: Any) -> str:
    text = _display(value).lower()
    if text in ("long", "bullish"):
        return "long"
    if text in ("short", "bearish"):
        return "short"
    return NA


def _first_non_na(*values: Any) -> str:
    for value in values:
        text = _display(value)
        if text != NA:
            return text
    return NA


def _maybe_decimal(value: Any) -> MaybeDecimal:
    if _is_missing(value):
        return NA
    try:
        return _quantize(_decimal_from(value, "portfolio decimal"))
    except ValueError:
        return NA


def _decimal_or_na(value: Any) -> MaybeDecimal:
    return _maybe_decimal(value)


def _decimal_score(value: Any) -> Decimal:
    decimal = _maybe_decimal(value)
    return Decimal("0") if decimal == NA else decimal


def _int_score(value: Any, *, default: int = 0, lower: int = 0, upper: int = 100) -> int:
    if _is_missing(value):
        return default
    try:
        decimal = _decimal_from(value, "portfolio score")
    except ValueError:
        return default
    return int(min(Decimal(upper), max(Decimal(lower), decimal)).to_integral_value(rounding="ROUND_HALF_UP"))


def _attr(source: Any, name: str | None = None) -> Any:
    if source is None:
        return NA
    if name is None:
        return source
    if isinstance(source, Mapping):
        return source.get(name, NA)
    value = getattr(source, name, NA)
    if isinstance(value, Enum):
        return value.value
    return value


def _sequence_values(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Sequence):
        return ()
    output: list[str] = []
    for value in values:
        text = _display(value)
        if text != NA and text not in output:
            output.append(text)
    return tuple(output)


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        text = _display(value)
        if text != NA and text not in output:
            output.append(text)
    return tuple(output)


def _display(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    if _is_missing(value):
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value).strip() or NA


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


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


__all__ = [
    "BetaGroup",
    "DEFAULT_MAX_BETA_GROUP_RISK_PCT",
    "DEFAULT_MAX_PORTFOLIO_RISK_PCT",
    "DEFAULT_MAX_SELECTED_SETUPS",
    "PortfolioCandidate",
    "PortfolioDecision",
    "PortfolioExposureGroup",
    "PortfolioRiskLimits",
    "PortfolioSelectionInput",
    "PortfolioSelectionResult",
    "build_portfolio_selection_from_scan",
    "format_portfolio_selection_summary",
    "select_portfolio",
    "selected_symbols",
]
