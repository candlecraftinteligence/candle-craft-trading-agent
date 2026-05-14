from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.data.dtos import NA, CandleDTO, FundingDTO, OpenInterestDTO, TickerDTO
from app.data.exceptions import ExchangeResponseError
from app.data.normalizers._helpers import (
    decimal_from,
    first_item,
    int_from,
    optional_decimal,
    require_field,
    require_index,
    require_list,
    require_mapping,
)

BYBIT_LINEAR_EXCHANGE = "bybit_linear"


def _result(payload: Any, path: str) -> dict[str, Any]:
    data = require_mapping(payload, path)
    return dict(require_mapping(require_field(data, "result", path), f"{path}.result"))


def normalize_bybit_klines(symbol: str, interval: str, payload: Any) -> list[CandleDTO]:
    result = _result(payload, "bybit.klines")
    rows = require_list(require_field(result, "list", "bybit.klines.result"), "bybit.klines.result.list")
    candles: list[CandleDTO] = []

    for index, raw in enumerate(rows):
        row_path = f"bybit.klines.result.list[{index}]"
        if not isinstance(raw, list):
            raise ExchangeResponseError(f"Expected list candle at {row_path}")

        candles.append(
            CandleDTO(
                exchange=BYBIT_LINEAR_EXCHANGE,
                symbol=symbol,
                timestamp=int_from(require_index(raw, 0, row_path), f"{row_path}[0]"),
                interval=interval,
                open=decimal_from(require_index(raw, 1, row_path), f"{row_path}[1]"),
                high=decimal_from(require_index(raw, 2, row_path), f"{row_path}[2]"),
                low=decimal_from(require_index(raw, 3, row_path), f"{row_path}[3]"),
                close=decimal_from(require_index(raw, 4, row_path), f"{row_path}[4]"),
                volume=decimal_from(require_index(raw, 5, row_path), f"{row_path}[5]"),
                quote_volume=optional_decimal(raw[6] if len(raw) > 6 else None, f"{row_path}[6]"),
                raw_source=raw,
            )
        )

    return sorted(candles, key=lambda candle: candle.timestamp)


def normalize_bybit_ticker(symbol: str, payload: Any) -> TickerDTO:
    top_level = require_mapping(payload, "bybit.ticker")
    result = _result(payload, "bybit.ticker")
    rows = require_list(require_field(result, "list", "bybit.ticker.result"), "bybit.ticker.result.list")
    data = require_mapping(first_item(rows, "bybit.ticker.result.list"), "bybit.ticker.result.list[0]")
    last_price = decimal_from(
        require_field(data, "lastPrice", "bybit.ticker.result.list[0]"),
        "bybit.ticker.result.list[0].lastPrice",
    )
    previous_price = optional_decimal(data.get("prevPrice24h"), "bybit.ticker.result.list[0].prevPrice24h")
    price_change = last_price - previous_price if isinstance(previous_price, Decimal) else NA

    return TickerDTO(
        exchange=BYBIT_LINEAR_EXCHANGE,
        symbol=str(require_field(data, "symbol", "bybit.ticker.result.list[0]")),
        timestamp=int_from(require_field(top_level, "time", "bybit.ticker"), "bybit.ticker.time"),
        last_price=last_price,
        high_price_24h=optional_decimal(data.get("highPrice24h"), "bybit.ticker.result.list[0].highPrice24h"),
        low_price_24h=optional_decimal(data.get("lowPrice24h"), "bybit.ticker.result.list[0].lowPrice24h"),
        volume_24h=optional_decimal(data.get("volume24h"), "bybit.ticker.result.list[0].volume24h"),
        quote_volume_24h=optional_decimal(data.get("turnover24h"), "bybit.ticker.result.list[0].turnover24h"),
        price_change_24h=price_change,
        price_change_ratio_24h=optional_decimal(data.get("price24hPcnt"), "bybit.ticker.result.list[0].price24hPcnt"),
        mark_price=optional_decimal(data.get("markPrice"), "bybit.ticker.result.list[0].markPrice"),
        index_price=optional_decimal(data.get("indexPrice"), "bybit.ticker.result.list[0].indexPrice"),
        bid_price=optional_decimal(data.get("bid1Price"), "bybit.ticker.result.list[0].bid1Price"),
        ask_price=optional_decimal(data.get("ask1Price"), "bybit.ticker.result.list[0].ask1Price"),
        raw_source=data,
    )


def normalize_bybit_funding(symbol: str, payload: Any) -> FundingDTO:
    result = _result(payload, "bybit.funding")
    rows = require_list(require_field(result, "list", "bybit.funding.result"), "bybit.funding.result.list")
    data = require_mapping(first_item(rows, "bybit.funding.result.list"), "bybit.funding.result.list[0]")

    return FundingDTO(
        exchange=BYBIT_LINEAR_EXCHANGE,
        symbol=str(require_field(data, "symbol", "bybit.funding.result.list[0]")),
        timestamp=int_from(
            require_field(data, "fundingRateTimestamp", "bybit.funding.result.list[0]"),
            "bybit.funding.result.list[0].fundingRateTimestamp",
        ),
        funding_rate=decimal_from(
            require_field(data, "fundingRate", "bybit.funding.result.list[0]"),
            "bybit.funding.result.list[0].fundingRate",
        ),
        raw_source=data,
    )


def normalize_bybit_open_interest(symbol: str, payload: Any) -> OpenInterestDTO:
    result = _result(payload, "bybit.open_interest")
    rows = require_list(
        require_field(result, "list", "bybit.open_interest.result"),
        "bybit.open_interest.result.list",
    )
    data = require_mapping(first_item(rows, "bybit.open_interest.result.list"), "bybit.open_interest.result.list[0]")

    return OpenInterestDTO(
        exchange=BYBIT_LINEAR_EXCHANGE,
        symbol=symbol,
        timestamp=int_from(
            require_field(data, "timestamp", "bybit.open_interest.result.list[0]"),
            "bybit.open_interest.result.list[0].timestamp",
        ),
        open_interest=decimal_from(
            require_field(data, "openInterest", "bybit.open_interest.result.list[0]"),
            "bybit.open_interest.result.list[0].openInterest",
        ),
        raw_source=data,
    )
