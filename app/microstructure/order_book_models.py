from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.context.models import ContextStatus
from app.data.candle_integrity import normalize_utc_timestamp


ORDER_BOOK_SOURCE = "binance_usdm_public_depth[st=1,rpi_excluded]"
ORDER_BOOK_USAGE = "research_only"


class LiquidityBandSnapshot(BaseModel):
    band_bps: int = Field(gt=0)
    bid_quote_notional: Decimal
    ask_quote_notional: Decimal
    depth_imbalance: Decimal | None = None
    bid_coverage_complete: bool
    ask_coverage_complete: bool

    model_config = ConfigDict(frozen=True)


class VisibleLevelConcentration(BaseModel):
    price: Decimal
    quote_notional: Decimal
    distance_bps: Decimal
    share_of_observed_band: Decimal | None = None
    band_bps: int = Field(gt=0)

    model_config = ConfigDict(frozen=True)


class OrderBookLiquiditySnapshot(BaseModel):
    symbol: str
    source: str = ORDER_BOOK_SOURCE
    usage: Literal["research_only"] = ORDER_BOOK_USAGE
    status: ContextStatus
    reason: str | None = None
    observed_at: datetime | None = None
    age_seconds: float | None = None
    synchronized: bool = False
    last_update_id: int | None = None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    mid_price: Decimal | None = None
    spread_absolute: Decimal | None = None
    spread_bps: Decimal | None = None
    bands: dict[str, LiquidityBandSnapshot] = Field(default_factory=dict)
    furthest_bid_distance_bps: Decimal | None = None
    furthest_ask_distance_bps: Decimal | None = None
    largest_bid_level: VisibleLevelConcentration | None = None
    largest_ask_level: VisibleLevelConcentration | None = None
    level_count_bid: int = Field(default=0, ge=0)
    level_count_ask: int = Field(default=0, ge=0)
    resync_count: int = Field(default=0, ge=0)
    gap_count: int = Field(default=0, ge=0)
    duplicate_event_count: int = Field(default=0, ge=0)
    out_of_order_event_count: int = Field(default=0, ge=0)
    buffer_overflow_count: int = Field(default=0, ge=0)

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = str(value).strip().upper()
        if not normalized:
            raise ValueError("order-book symbol must not be blank")
        return normalized

    @field_validator("source")
    @classmethod
    def _source_not_blank(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("order-book source must not be blank")
        return normalized

    @field_validator("observed_at", mode="before")
    @classmethod
    def _normalize_observed_at(cls, value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        return normalize_utc_timestamp(value, field_name="order_book_observed_at")

    @field_validator("age_seconds")
    @classmethod
    def _age_non_negative(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return round(max(float(value), 0.0), 3)

    @property
    def verified(self) -> bool:
        return self.status == ContextStatus.VERIFIED and self.synchronized

    def liquidity_below_context(self) -> dict[str, Any] | None:
        return self._side_context("bid")

    def liquidity_above_context(self) -> dict[str, Any] | None:
        return self._side_context("ask")

    def _side_context(self, side: Literal["bid", "ask"]) -> dict[str, Any] | None:
        if self.status not in {ContextStatus.VERIFIED, ContextStatus.STALE}:
            return None
        context: dict[str, Any] = {
            "usage": ORDER_BOOK_USAGE,
            "source": self.source,
            "status": self.status.value,
            "reason": self.reason,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "age_seconds": self.age_seconds,
            "side": side,
        }
        if not self.verified:
            context["summary"] = f"Visible {side} depth is unverified: {self.reason or 'stale_book'}."
            return context

        context.update(
            mid_price=self.mid_price,
            bands={
                label: {
                    "quote_notional": (
                        band.bid_quote_notional if side == "bid" else band.ask_quote_notional
                    ),
                    "coverage_complete": (
                        band.bid_coverage_complete if side == "bid" else band.ask_coverage_complete
                    ),
                }
                for label, band in self.bands.items()
            },
            largest_level=(
                self.largest_bid_level.model_dump(mode="python")
                if side == "bid" and self.largest_bid_level is not None
                else self.largest_ask_level.model_dump(mode="python")
                if side == "ask" and self.largest_ask_level is not None
                else None
            ),
            summary=(
                "Observed visible bid depth below book mid; research only."
                if side == "bid"
                else "Observed visible ask depth above book mid; research only."
            ),
        )
        return context

    @classmethod
    def unavailable(
        cls,
        *,
        symbol: str,
        reason: str,
        status: ContextStatus = ContextStatus.UNAVAILABLE,
        observed_at: datetime | None = None,
        age_seconds: float | None = None,
        resync_count: int = 0,
        gap_count: int = 0,
        duplicate_event_count: int = 0,
        out_of_order_event_count: int = 0,
        buffer_overflow_count: int = 0,
    ) -> OrderBookLiquiditySnapshot:
        return cls(
            symbol=symbol,
            status=status,
            reason=reason,
            observed_at=observed_at,
            age_seconds=age_seconds,
            resync_count=resync_count,
            gap_count=gap_count,
            duplicate_event_count=duplicate_event_count,
            out_of_order_event_count=out_of_order_event_count,
            buffer_overflow_count=buffer_overflow_count,
        )
