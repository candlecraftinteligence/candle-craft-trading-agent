from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.context.models import ContextStatus
from app.data.candle_integrity import normalize_utc_timestamp


MICROSTRUCTURE_SOURCE = "binance_usdm:{symbol}@aggTrade"


class PriceCvdAlignment(str, Enum):
    ALIGNED_UP = "ALIGNED_UP"
    ALIGNED_DOWN = "ALIGNED_DOWN"
    PRICE_UP_CVD_DOWN = "PRICE_UP_CVD_DOWN"
    PRICE_DOWN_CVD_UP = "PRICE_DOWN_CVD_UP"
    MIXED_FLAT = "MIXED_FLAT"


class FlowWindowSnapshot(BaseModel):
    window_minutes: int = Field(ge=1)
    window_start: datetime
    window_end: datetime
    coverage_seconds: int = Field(ge=0)
    coverage_complete: bool
    reason: str | None = None
    aggressive_buy_base: Decimal | None = None
    aggressive_sell_base: Decimal | None = None
    aggressive_buy_quote: Decimal | None = None
    aggressive_sell_quote: Decimal | None = None
    delta_base: Decimal | None = None
    delta_quote: Decimal | None = None
    total_quote: Decimal | None = None
    flow_imbalance_ratio: Decimal | None = None
    buyer_aggression_pct: Decimal | None = None
    rolling_cvd_quote: Decimal | None = None
    cvd_slope_quote_per_min: Decimal | None = None
    price_return_pct: Decimal | None = None
    price_cvd_alignment: PriceCvdAlignment | None = None
    normal_quote_notional: Decimal | None = None
    rpi_quote_notional: Decimal | None = None
    aggregate_event_count: int = Field(default=0, ge=0)
    underlying_trade_count: int = Field(default=0, ge=0)

    model_config = ConfigDict(frozen=True)

    @field_validator("window_start", "window_end", mode="before")
    @classmethod
    def _normalize_timestamps(cls, value: Any) -> datetime:
        return normalize_utc_timestamp(value, field_name="microstructure_window_timestamp")


class MicrostructureFlowSnapshot(BaseModel):
    symbol: str
    source: str
    observed_at: datetime | None = None
    age_seconds: float | None = None
    status: ContextStatus
    reason: str | None = None
    usage: Literal["research_only"] = "research_only"
    windows: dict[str, FlowWindowSnapshot] = Field(default_factory=dict)
    orderflow_summary: str
    retained_bucket_count: int = Field(default=0, ge=0)
    max_retained_bucket_count: int = Field(default=0, ge=0)
    last_aggregate_trade_id: int | None = None
    accepted_event_count: int = Field(default=0, ge=0)
    duplicate_event_count: int = Field(default=0, ge=0)
    out_of_order_event_count: int = Field(default=0, ge=0)
    gap_count: int = Field(default=0, ge=0)
    reconnect_count: int = Field(default=0, ge=0)
    rpi_event_count: int = Field(default=0, ge=0)

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = str(value).strip().upper()
        if not normalized:
            raise ValueError("microstructure symbol must not be blank")
        return normalized

    @field_validator("source")
    @classmethod
    def _source_not_blank(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("microstructure source must not be blank")
        return normalized

    @field_validator("observed_at", mode="before")
    @classmethod
    def _normalize_observed_at(cls, value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        return normalize_utc_timestamp(value, field_name="microstructure_observed_at")

    @field_validator("age_seconds")
    @classmethod
    def _age_non_negative(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return round(max(float(value), 0.0), 3)

    @property
    def verified(self) -> bool:
        return self.status == ContextStatus.VERIFIED

    def cvd_strategy_context(self) -> dict[str, Any] | None:
        if not self.verified:
            return None
        window_5m = self.windows.get("5m")
        window_15m = self.windows.get("15m")
        if window_5m is None or window_15m is None:
            return None
        return {
            "usage": "research_only",
            "status": self.status.value,
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "value": {
                "rolling_cvd_quote_5m": window_5m.rolling_cvd_quote,
                "rolling_cvd_quote_15m": window_15m.rolling_cvd_quote,
                "cvd_slope_quote_per_min_5m": window_5m.cvd_slope_quote_per_min,
                "cvd_slope_quote_per_min_15m": window_15m.cvd_slope_quote_per_min,
            },
            "summary": (
                f"15m rolling CVD {window_15m.rolling_cvd_quote} USDT; "
                f"slope {window_15m.cvd_slope_quote_per_min} USDT/min"
            ),
        }

    def orderflow_strategy_context(self) -> dict[str, Any] | None:
        if not self.verified:
            return None
        return {
            "usage": "research_only",
            "status": self.status.value,
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "summary": self.orderflow_summary,
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
    ) -> MicrostructureFlowSnapshot:
        normalized_symbol = str(symbol).strip().upper()
        return cls(
            symbol=normalized_symbol,
            source=MICROSTRUCTURE_SOURCE.format(symbol=normalized_symbol.lower()),
            observed_at=observed_at,
            age_seconds=age_seconds,
            status=status,
            reason=reason,
            orderflow_summary=f"Orderflow unavailable: {reason}.",
        )
