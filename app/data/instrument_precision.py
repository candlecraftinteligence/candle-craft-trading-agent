"""Public exchange tick-size provenance, independent of derivatives policy."""
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class InstrumentPrecision(BaseModel):
    exchange: str
    symbol: str
    tick_size: Decimal
    source_endpoint: str
    received_at: datetime

    model_config = ConfigDict(frozen=True)

    def verified_for(self, exchange: str, symbol: str, at: datetime) -> bool:
        return (self.exchange == exchange and self.symbol == symbol and self.source_endpoint in {
            "/fapi/v1/exchangeInfo", "/v5/market/instruments-info"
        } and self.tick_size.is_finite() and self.tick_size > 0 and self.received_at.tzinfo is not None
            and 0 <= (at - self.received_at).total_seconds() <= 300)


def parse_precision(payload: Any, *, exchange: str, symbol: str) -> InstrumentPrecision:
    if not isinstance(payload, Mapping):
        raise ValueError("precision_response_not_object")
    if exchange == "binance":
        rows = payload.get("symbols", ())
        endpoint = "/fapi/v1/exchangeInfo"
    elif exchange == "bybit":
        if str(payload.get("retCode")) != "0" or payload.get("result", {}).get("category") != "linear":
            raise ValueError("precision_response_invalid")
        rows = payload.get("result", {}).get("list", ())
        endpoint = "/v5/market/instruments-info"
    else:
        raise ValueError("precision_exchange_unsupported")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("symbol") == symbol]
    if len(matches) != 1:
        raise ValueError("precision_symbol_unverified")
    row = matches[0]
    if exchange == "binance":
        filters = [f for f in row.get("filters", ()) if isinstance(f, Mapping) and f.get("filterType") == "PRICE_FILTER"]
        if len(filters) != 1:
            raise ValueError("precision_filter_unverified")
        raw = filters[0].get("tickSize")
    else:
        raw = row.get("priceFilter", {}).get("tickSize")
    if isinstance(raw, (bool, float)):
        raise ValueError("precision_tick_unverified")
    tick = Decimal(str(raw))
    if not tick.is_finite() or tick <= 0:
        raise ValueError("precision_tick_unverified")
    return InstrumentPrecision(exchange=exchange, symbol=symbol, tick_size=tick,
                               source_endpoint=endpoint, received_at=datetime.now(UTC))
