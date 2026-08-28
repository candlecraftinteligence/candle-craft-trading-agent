from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.context.models import ContextStatus
from app.data.candle_integrity import normalize_utc_timestamp


LIQUIDATION_SOURCE = "binance_futures:!forceOrder@arr[st=1]"


class LiquidationAcceleration(str, Enum):
    INCREASING = "INCREASING"
    DECREASING = "DECREASING"
    STABLE = "STABLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class LiquidationAccelerationSnapshot(BaseModel):
    status: LiquidationAcceleration
    recent_minutes: int = Field(ge=1)
    prior_minutes: int = Field(ge=1)
    recent_quote_per_minute: Decimal | None = None
    prior_quote_per_minute: Decimal | None = None
    recent_vs_prior_ratio: Decimal | None = None
    reason: str | None = None

    model_config = ConfigDict(frozen=True)


class LiquidationWindowSnapshot(BaseModel):
    window_minutes: int = Field(ge=1)
    window_start: datetime
    window_end: datetime
    coverage_seconds: int = Field(ge=0)
    coverage_complete: bool
    status: ContextStatus
    reason: str | None = None
    long_liquidation_quote: Decimal | None = None
    short_liquidation_quote: Decimal | None = None
    total_liquidation_quote: Decimal | None = None
    event_count: int | None = Field(default=None, ge=0)
    long_event_count: int | None = Field(default=None, ge=0)
    short_event_count: int | None = Field(default=None, ge=0)
    largest_long_liquidation: Decimal | None = None
    largest_short_liquidation: Decimal | None = None
    liquidation_imbalance: Decimal | None = None
    liquidation_quote_per_minute: Decimal | None = None
    liquidation_event_count_per_minute: Decimal | None = None
    largest_event_share_of_total: Decimal | None = None
    acceleration: LiquidationAccelerationSnapshot | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("window_start", "window_end", mode="before")
    @classmethod
    def _normalize_timestamps(cls, value: Any) -> datetime:
        return normalize_utc_timestamp(value, field_name="liquidation_window_timestamp")


class LiquidationFlowSnapshot(BaseModel):
    symbol: str
    source: str = LIQUIDATION_SOURCE
    observed_at: datetime | None = None
    age_seconds: float | None = None
    status: ContextStatus
    reason: str | None = None
    usage: Literal["research_only"] = "research_only"
    windows: dict[str, LiquidationWindowSnapshot] = Field(default_factory=dict)
    liquidation_summary: str
    retained_bucket_count: int = Field(default=0, ge=0)
    max_retained_bucket_count: int = Field(default=0, ge=0)
    dedupe_fingerprint_count: int = Field(default=0, ge=0)
    max_dedupe_fingerprint_count: int = Field(default=0, ge=0)
    accepted_event_count: int = Field(default=0, ge=0)
    duplicate_event_count: int = Field(default=0, ge=0)
    malformed_event_count: int = Field(default=0, ge=0)
    stale_event_count: int = Field(default=0, ge=0)
    wrong_contract_event_count: int = Field(default=0, ge=0)
    untracked_symbol_event_count: int = Field(default=0, ge=0)
    connection_count: int = Field(default=0, ge=0)
    disconnect_count: int = Field(default=0, ge=0)
    reconnect_count: int = Field(default=0, ge=0)

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = str(value).strip().upper()
        if not normalized:
            raise ValueError("liquidation symbol must not be blank")
        return normalized

    @field_validator("observed_at", mode="before")
    @classmethod
    def _normalize_observed_at(cls, value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        return normalize_utc_timestamp(value, field_name="liquidation_observed_at")

    @field_validator("age_seconds")
    @classmethod
    def _age_non_negative(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return round(max(float(value), 0.0), 3)

    @property
    def verified(self) -> bool:
        return self.status == ContextStatus.VERIFIED

    def strategy_context(self) -> dict[str, Any] | None:
        """Return compact strategy-visible research context, never a strategy signal."""

        if self.status not in {ContextStatus.VERIFIED, ContextStatus.STALE}:
            return None
        return {
            "usage": "research_only",
            "status": self.status.value,
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "summary": self.liquidation_summary,
        }

    @classmethod
    def unavailable(
        cls,
        *,
        symbol: str,
        reason: str,
        status: ContextStatus = ContextStatus.UNAVAILABLE,
        observed_at: datetime | None = None,
        age_seconds: float | None = None,
    ) -> LiquidationFlowSnapshot:
        normalized = str(symbol).strip().upper()
        return cls(
            symbol=normalized,
            observed_at=observed_at,
            age_seconds=age_seconds,
            status=status,
            reason=reason,
            liquidation_summary=f"Liquidation flow unavailable: {reason}.",
        )
