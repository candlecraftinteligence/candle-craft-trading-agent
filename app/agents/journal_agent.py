from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.data.dtos import NA, MaybeDecimal

Direction = Literal["long", "short"]
OUTPUT_QUANT = Decimal("0.00000001")


class JournalStatus(str, Enum):
    WATCHING = "watching"
    TRIGGERED = "triggered"
    INVALIDATED = "invalidated"
    TP1_HIT = "tp1_hit"
    TP2_HIT = "tp2_hit"
    TP3_HIT = "tp3_hit"
    STOPPED = "stopped"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class JournalEntryInput(BaseModel):
    trade_idea_id: str | int | None = None
    alert_id: str | int | None = None
    symbol: str
    exchange: str | None = None
    direction: Direction
    timeframe: str
    setup_type: str
    status: JournalStatus
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    take_profit_targets: tuple[Decimal, ...]
    invalidation: str
    best_rr: Decimal
    confidence_score: Decimal
    grade: str
    reason_for_trade: str
    confirmed_facts: tuple[str, ...] | None = None
    missing_data: tuple[str, ...] | None = None
    unverified_data: tuple[str, ...] | None = None
    risk_warning: str
    screenshot_url: str | None = None
    notes: str | None = None
    emotional_notes: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator(
        "symbol",
        "timeframe",
        "setup_type",
        "invalidation",
        "grade",
        "reason_for_trade",
        "risk_warning",
    )
    @classmethod
    def _required_text(cls, value: str) -> str:
        if value.strip() == "":
            raise ValueError("field must not be blank")
        return value.strip()

    @field_validator("entry_low", "entry_high", "stop_loss", "best_rr", "confidence_score")
    @classmethod
    def _decimal_is_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("decimal value must be finite")
        return value

    @field_validator("take_profit_targets")
    @classmethod
    def _targets_are_finite(cls, value: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
        for target in value:
            if not target.is_finite():
                raise ValueError("take profit targets must be finite")
        return value

    @field_validator("confirmed_facts", "missing_data", "unverified_data", mode="before")
    @classmethod
    def _normalize_string_tuple(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            return (value,)
        return value


class JournalUpdateInput(BaseModel):
    status: JournalStatus | None = None
    result_r: Decimal | None = None
    notes: str | None = None
    emotional_notes: str | None = None
    screenshot_url: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("result_r")
    @classmethod
    def _optional_decimal_is_finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("decimal value must be finite")
        return value


class JournalEntryResult(BaseModel):
    trade_idea_id: str | int | None = None
    alert_id: str | int | None = None
    symbol: str
    exchange: str
    direction: Direction
    timeframe: str
    setup_type: str
    status: JournalStatus
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    take_profit_targets: tuple[Decimal, ...]
    invalidation: str
    best_rr: Decimal
    confidence_score: Decimal
    grade: str
    reason_for_trade: str
    confirmed_facts: tuple[str, ...]
    missing_data: tuple[str, ...]
    unverified_data: tuple[str, ...]
    risk_warning: str
    screenshot_url: str
    notes: str
    emotional_notes: str
    result_r: MaybeDecimal = NA

    model_config = ConfigDict(frozen=True)


class SetupPerformanceStats(BaseModel):
    setup_type: str
    total_entries: int
    resolved_count: int
    win_count: int
    loss_count: int
    win_rate: MaybeDecimal = NA
    average_r: MaybeDecimal = NA
    best_r: MaybeDecimal = NA
    worst_r: MaybeDecimal = NA

    model_config = ConfigDict(frozen=True)


class PerformanceSummary(BaseModel):
    total_entries: int
    triggered_count: int
    invalidated_count: int
    stopped_count: int
    closed_count: int
    win_count: int
    loss_count: int
    win_rate: MaybeDecimal = NA
    average_r: MaybeDecimal = NA
    best_r: MaybeDecimal = NA
    worst_r: MaybeDecimal = NA
    best_setup_type: str = NA
    worst_setup_type: str = NA
    setup_stats: tuple[SetupPerformanceStats, ...] = ()

    model_config = ConfigDict(frozen=True)


class JournalAgent:
    """Create and update in-memory journal entries for ideas and alerts.

    The journal agent persists no data in Phase 9. It does not call exchanges,
    use private API access, place orders, or execute trades.
    """

    def create(
        self,
        entry: JournalEntryInput | Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> JournalEntryResult:
        journal_input = _normalize_entry_input(entry, overrides)
        return JournalEntryResult(
            trade_idea_id=journal_input.trade_idea_id,
            alert_id=journal_input.alert_id,
            symbol=journal_input.symbol,
            exchange=_optional_text(journal_input.exchange),
            direction=journal_input.direction,
            timeframe=journal_input.timeframe,
            setup_type=journal_input.setup_type,
            status=journal_input.status,
            entry_low=_quantize(journal_input.entry_low),
            entry_high=_quantize(journal_input.entry_high),
            stop_loss=_quantize(journal_input.stop_loss),
            take_profit_targets=_quantize_many(journal_input.take_profit_targets),
            invalidation=journal_input.invalidation,
            best_rr=_quantize(journal_input.best_rr),
            confidence_score=_quantize(journal_input.confidence_score),
            grade=journal_input.grade,
            reason_for_trade=journal_input.reason_for_trade,
            confirmed_facts=_string_tuple_or_na(journal_input.confirmed_facts),
            missing_data=_string_tuple_or_na(journal_input.missing_data),
            unverified_data=_string_tuple_or_na(journal_input.unverified_data),
            risk_warning=journal_input.risk_warning,
            screenshot_url=_optional_text(journal_input.screenshot_url),
            notes=_optional_text(journal_input.notes),
            emotional_notes=_optional_text(journal_input.emotional_notes),
        )

    def update(
        self,
        entry: JournalEntryResult | Mapping[str, Any],
        update: JournalUpdateInput | Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> JournalEntryResult:
        journal_entry = _normalize_existing_entry(entry)
        update_input = _normalize_update_input(update, overrides)
        fields_set = update_input.model_fields_set
        changes: dict[str, Any] = {}

        if "status" in fields_set and update_input.status is not None:
            changes["status"] = update_input.status
        if "result_r" in fields_set:
            changes["result_r"] = NA if update_input.result_r is None else _quantize(update_input.result_r)
        if "notes" in fields_set:
            changes["notes"] = _optional_text(update_input.notes)
        if "emotional_notes" in fields_set:
            changes["emotional_notes"] = _optional_text(update_input.emotional_notes)
        if "screenshot_url" in fields_set:
            changes["screenshot_url"] = _optional_text(update_input.screenshot_url)

        return journal_entry.model_copy(update=changes)

    def summarize(
        self,
        entries: Sequence[JournalEntryResult | Mapping[str, Any]],
    ) -> PerformanceSummary:
        journal_entries = tuple(_normalize_existing_entry(entry) for entry in entries)
        result_rs = tuple(entry.result_r for entry in journal_entries if entry.result_r != NA)
        resolved_result_rs = tuple(result_r for result_r in result_rs if result_r != 0)
        setup_stats = _setup_stats(journal_entries)

        best_setup = _best_setup_type(setup_stats)
        worst_setup = _worst_setup_type(setup_stats)

        return PerformanceSummary(
            total_entries=len(journal_entries),
            triggered_count=_count_status(journal_entries, JournalStatus.TRIGGERED),
            invalidated_count=_count_status(journal_entries, JournalStatus.INVALIDATED),
            stopped_count=_count_status(journal_entries, JournalStatus.STOPPED),
            closed_count=_count_status(journal_entries, JournalStatus.CLOSED),
            win_count=sum(1 for result_r in resolved_result_rs if result_r > 0),
            loss_count=sum(1 for result_r in resolved_result_rs if result_r < 0),
            win_rate=_win_rate(resolved_result_rs),
            average_r=_average(result_rs),
            best_r=_max_or_na(result_rs),
            worst_r=_min_or_na(result_rs),
            best_setup_type=best_setup,
            worst_setup_type=worst_setup,
            setup_stats=setup_stats,
        )

    def analyze(
        self,
        entries: Sequence[JournalEntryResult | Mapping[str, Any]],
    ) -> PerformanceSummary:
        return self.summarize(entries)


def create_journal_entry(
    entry: JournalEntryInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> JournalEntryResult:
    return JournalAgent().create(entry, **overrides)


def update_journal_entry(
    entry: JournalEntryResult | Mapping[str, Any],
    update: JournalUpdateInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> JournalEntryResult:
    return JournalAgent().update(entry, update, **overrides)


def summarize_performance(
    entries: Sequence[JournalEntryResult | Mapping[str, Any]],
) -> PerformanceSummary:
    return JournalAgent().summarize(entries)


def _normalize_entry_input(
    entry: JournalEntryInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> JournalEntryInput:
    if entry is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(entry, JournalEntryInput):
        raw = entry.model_dump()
        raw.update(overrides)
    else:
        raw = dict(entry)
        raw.update(overrides)
    return JournalEntryInput.model_validate(raw)


def _normalize_update_input(
    update: JournalUpdateInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> JournalUpdateInput:
    if update is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(update, JournalUpdateInput):
        raw = update.model_dump(exclude_unset=True)
        raw.update(overrides)
    else:
        raw = dict(update)
        raw.update(overrides)
    return JournalUpdateInput.model_validate(raw)


def _normalize_existing_entry(entry: JournalEntryResult | Mapping[str, Any]) -> JournalEntryResult:
    if isinstance(entry, JournalEntryResult):
        return entry
    return JournalEntryResult.model_validate(entry)


def _setup_stats(entries: tuple[JournalEntryResult, ...]) -> tuple[SetupPerformanceStats, ...]:
    stats: list[SetupPerformanceStats] = []
    setup_types = sorted({entry.setup_type for entry in entries})
    for setup_type in setup_types:
        setup_entries = tuple(entry for entry in entries if entry.setup_type == setup_type)
        result_rs = tuple(entry.result_r for entry in setup_entries if entry.result_r != NA)
        resolved_result_rs = tuple(result_r for result_r in result_rs if result_r != 0)
        stats.append(
            SetupPerformanceStats(
                setup_type=setup_type,
                total_entries=len(setup_entries),
                resolved_count=len(resolved_result_rs),
                win_count=sum(1 for result_r in resolved_result_rs if result_r > 0),
                loss_count=sum(1 for result_r in resolved_result_rs if result_r < 0),
                win_rate=_win_rate(resolved_result_rs),
                average_r=_average(result_rs),
                best_r=_max_or_na(result_rs),
                worst_r=_min_or_na(result_rs),
            )
        )
    return tuple(stats)


def _best_setup_type(stats: tuple[SetupPerformanceStats, ...]) -> str:
    resolved = tuple(stat for stat in stats if stat.average_r != NA)
    if not resolved:
        return NA
    return max(resolved, key=lambda stat: (stat.average_r, stat.setup_type)).setup_type


def _worst_setup_type(stats: tuple[SetupPerformanceStats, ...]) -> str:
    resolved = tuple(stat for stat in stats if stat.average_r != NA)
    if not resolved:
        return NA
    return min(resolved, key=lambda stat: (stat.average_r, stat.setup_type)).setup_type


def _count_status(entries: tuple[JournalEntryResult, ...], status: JournalStatus) -> int:
    return sum(1 for entry in entries if entry.status == status)


def _win_rate(result_rs: tuple[Decimal, ...]) -> MaybeDecimal:
    wins = sum(1 for result_r in result_rs if result_r > 0)
    losses = sum(1 for result_r in result_rs if result_r < 0)
    total = wins + losses
    if total == 0:
        return NA
    return _quantize(Decimal(wins) / Decimal(total) * Decimal("100"))


def _average(values: tuple[Decimal, ...]) -> MaybeDecimal:
    if not values:
        return NA
    return _quantize(sum(values, Decimal("0")) / Decimal(len(values)))


def _max_or_na(values: tuple[Decimal, ...]) -> MaybeDecimal:
    if not values:
        return NA
    return _quantize(max(values))


def _min_or_na(values: tuple[Decimal, ...]) -> MaybeDecimal:
    if not values:
        return NA
    return _quantize(min(values))


def _quantize_many(values: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
    return tuple(_quantize(value) for value in values)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _optional_text(value: str | None) -> str:
    if value is None or value.strip() == "":
        return NA
    return value.strip()


def _string_tuple_or_na(values: tuple[str, ...] | None) -> tuple[str, ...]:
    if values is None:
        return (NA,)
    cleaned = tuple(value.strip() for value in values if value.strip())
    if not cleaned:
        return (NA,)
    return cleaned


__all__ = [
    "JournalAgent",
    "JournalEntryInput",
    "JournalEntryResult",
    "JournalStatus",
    "JournalUpdateInput",
    "PerformanceSummary",
    "SetupPerformanceStats",
    "create_journal_entry",
    "summarize_performance",
    "update_journal_entry",
]
