from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

NA = "N/A"

MaybeDecimal: TypeAlias = Decimal | Literal["N/A"]
MaybeInt: TypeAlias = int | Literal["N/A"]


class MarketDataDTO(BaseModel):
    exchange: str
    symbol: str
    raw_source: Any | None = None

    model_config = ConfigDict(frozen=True)


class CandleDTO(MarketDataDTO):
    timestamp: int
    interval: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_timestamp: MaybeInt = NA
    quote_volume: MaybeDecimal = NA
    trade_count: MaybeInt = NA


class TickerDTO(MarketDataDTO):
    timestamp: MaybeInt = NA
    last_price: Decimal
    high_price_24h: MaybeDecimal = NA
    low_price_24h: MaybeDecimal = NA
    volume_24h: MaybeDecimal = NA
    quote_volume_24h: MaybeDecimal = NA
    price_change_24h: MaybeDecimal = NA
    price_change_ratio_24h: MaybeDecimal = NA
    mark_price: MaybeDecimal = NA
    index_price: MaybeDecimal = NA
    bid_price: MaybeDecimal = NA
    ask_price: MaybeDecimal = NA


class FundingDTO(MarketDataDTO):
    timestamp: int
    funding_rate: Decimal
    mark_price: MaybeDecimal = NA
    next_funding_time: MaybeInt = NA


class OpenInterestDTO(MarketDataDTO):
    timestamp: MaybeInt = NA
    open_interest: Decimal
    notional_value: MaybeDecimal = NA
    unit: str = NA
