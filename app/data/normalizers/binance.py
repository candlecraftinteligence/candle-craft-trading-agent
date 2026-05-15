from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.data.dtos import NA, CandleDTO, FundingDTO, OpenInterestDTO, TickerDTO
from app.data.exceptions import ExchangeResponseError
from app.data.normalizers._helpers import (
    decimal_from,
    int_from,
    last_item,
    optional_decimal,
    require_field,
    require_index,
    require_list,
    require_mapping,
)

BINANCE_FUTURES_EXCHANGE = "binance_futures"


def normalize_binance_klines(symbol: str, interval: str, payload: Any) -> list[CandleDTO]:
    rows = require_list(payload, "binance.klines")
    candles: list[CandleDTO] = []

    for index, raw in enumerate(rows):
        row_path = f"binance.klines[{index}]"
        if not isinstance(raw, list):
            raise ExchangeResponseError(f"Expected list candle at {row_path}")

        candles.append(
            CandleDTO(
                exchange=BINANCE_FUTURES_EXCHANGE,
                symbol=symbol,
                timestamp=int_from(require_index(raw, 0, row_path), f"{row_path}[0]"),
                interval=interval,
                open=decimal_from(require_index(raw, 1, row_path), f"{row_path}[1]"),
                high=decimal_from(require_index(raw, 2, row_path), f"{row_path}[2]"),
                low=decimal_from(require_index(raw, 3, row_path), f"{row_path}[3]"),
                close=decimal_from(require_index(raw, 4, row_path), f"{row_path}[4]"),
                volume=decimal_from(require_index(raw, 5, row_path), f"{row_path}[5]"),
                close_timestamp=int_from(require_index(raw, 6, row_path), f"{row_path}[6]"),
                quote_volume=optional_decimal(raw[7] if len(raw) > 7 else None, f"{row_path}[7]"),
                trade_count=int_from(require_index(raw, 8, row_path), f"{row_path}[8]")
                if len(raw) > 8
                else NA,
                raw_source=raw,
            )
        )

    return candles


def normalize_binance_ticker(symbol: str, payload: Any) -> TickerDTO:
    data = require_mapping(payload, "binance.ticker")
    price_change_percent = optional_decimal(
        data.get("priceChangePercent"),
        "binance.ticker.priceChangePercent",
    )
    price_change_ratio = (
        price_change_percent / Decimal("100")
        if isinstance(price_change_percent, Decimal)
        else NA
    )

    return TickerDTO(
        exchange=BINANCE_FUTURES_EXCHANGE,
        symbol=str(require_field(data, "symbol", "binance.ticker")),
        timestamp=int_from(require_field(data, "closeTime", "binance.ticker"), "binance.ticker.closeTime"),
        last_price=decimal_from(
            require_field(data, "lastPrice", "binance.ticker"),
            "binance.ticker.lastPrice",
        ),
        high_price_24h=optional_decimal(data.get("highPrice"), "binance.ticker.highPrice"),
        low_price_24h=optional_decimal(data.get("lowPrice"), "binance.ticker.lowPrice"),
        volume_24h=optional_decimal(data.get("volume"), "binance.ticker.volume"),
        quote_volume_24h=optional_decimal(data.get("quoteVolume"), "binance.ticker.quoteVolume"),
        price_change_24h=optional_decimal(data.get("priceChange"), "binance.ticker.priceChange"),
        price_change_ratio_24h=price_change_ratio,
        raw_source=data,
    )


def normalize_binance_funding(symbol: str, payload: Any) -> FundingDTO:
    rows = require_list(payload, "binance.funding")
    data = require_mapping(last_item(rows, "binance.funding"), "binance.funding[-1]")

    return FundingDTO(
        exchange=BINANCE_FUTURES_EXCHANGE,
        symbol=str(require_field(data, "symbol", "binance.funding[-1]")),
        timestamp=int_from(
            require_field(data, "fundingTime", "binance.funding[-1]"),
            "binance.funding[-1].fundingTime",
        ),
        funding_rate=decimal_from(
            require_field(data, "fundingRate", "binance.funding[-1]"),
            "binance.funding[-1].fundingRate",
        ),
        mark_price=optional_decimal(data.get("markPrice"), "binance.funding[-1].markPrice"),
        raw_source=data,
    )


def normalize_binance_funding_history(symbol: str, payload: Any) -> list[FundingDTO]:
    rows = require_list(payload, "binance.funding")
    funding: list[FundingDTO] = []

    for index, raw in enumerate(rows):
        row_path = f"binance.funding[{index}]"
        data = require_mapping(raw, row_path)
        funding.append(
            FundingDTO(
                exchange=BINANCE_FUTURES_EXCHANGE,
                symbol=str(require_field(data, "symbol", row_path)),
                timestamp=int_from(
                    require_field(data, "fundingTime", row_path),
                    f"{row_path}.fundingTime",
                ),
                funding_rate=decimal_from(
                    require_field(data, "fundingRate", row_path),
                    f"{row_path}.fundingRate",
                ),
                mark_price=optional_decimal(data.get("markPrice"), f"{row_path}.markPrice"),
                raw_source=data,
            )
        )

    return sorted(funding, key=lambda item: item.timestamp)


def normalize_binance_open_interest(symbol: str, payload: Any) -> OpenInterestDTO:
    data = require_mapping(payload, "binance.open_interest")

    return OpenInterestDTO(
        exchange=BINANCE_FUTURES_EXCHANGE,
        symbol=str(require_field(data, "symbol", "binance.open_interest")),
        timestamp=int_from(
            require_field(data, "time", "binance.open_interest"),
            "binance.open_interest.time",
        ),
        open_interest=decimal_from(
            require_field(data, "openInterest", "binance.open_interest"),
            "binance.open_interest.openInterest",
        ),
        raw_source=data,
    )


def normalize_binance_open_interest_history(symbol: str, payload: Any) -> list[OpenInterestDTO]:
    rows = require_list(payload, "binance.open_interest_history")
    history: list[OpenInterestDTO] = []

    for index, raw in enumerate(rows):
        row_path = f"binance.open_interest_history[{index}]"
        data = require_mapping(raw, row_path)
        history.append(
            OpenInterestDTO(
                exchange=BINANCE_FUTURES_EXCHANGE,
                symbol=symbol,
                timestamp=int_from(
                    require_field(data, "timestamp", row_path),
                    f"{row_path}.timestamp",
                ),
                open_interest=decimal_from(
                    require_field(data, "sumOpenInterest", row_path),
                    f"{row_path}.sumOpenInterest",
                ),
                notional_value=optional_decimal(data.get("sumOpenInterestValue"), f"{row_path}.sumOpenInterestValue"),
                raw_source=data,
            )
        )

    return sorted(history, key=lambda item: item.timestamp if isinstance(item.timestamp, int) else 0)


def normalize_binance_long_short_ratio(symbol: str, payload: Any) -> Decimal:
    rows = require_list(payload, "binance.long_short_ratio")
    data = require_mapping(last_item(rows, "binance.long_short_ratio"), "binance.long_short_ratio[-1]")
    return decimal_from(
        require_field(data, "longShortRatio", "binance.long_short_ratio[-1]"),
        "binance.long_short_ratio[-1].longShortRatio",
    )
