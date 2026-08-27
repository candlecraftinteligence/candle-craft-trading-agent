from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict


UM_STREAM_TYPE = 1
CM_STREAM_TYPE = 2


class AggTradePayloadError(ValueError):
    """Raised when an aggregate-trade payload cannot be trusted."""


class WrongContractTypeError(AggTradePayloadError):
    """Raised when a detectable COIN-M payload reaches the UM parser."""


class BinanceAggTrade(BaseModel):
    symbol: str
    aggregate_trade_id: int
    price: Decimal
    quantity: Decimal
    normal_quantity: Decimal | None
    first_trade_id: int
    last_trade_id: int
    event_time: datetime
    trade_time: datetime
    buyer_is_maker: bool
    stream_type: int | None

    model_config = ConfigDict(frozen=True)

    @property
    def aggressive_side(self) -> str:
        # Binance m=true means buyer is maker, so the seller is the taker/aggressor.
        return "SELL" if self.buyer_is_maker else "BUY"

    @property
    def quote_notional(self) -> Decimal:
        return self.price * self.quantity

    @property
    def normal_quote_notional(self) -> Decimal | None:
        if self.normal_quantity is None:
            return None
        return self.price * self.normal_quantity

    @property
    def underlying_trade_count(self) -> int:
        return self.last_trade_id - self.first_trade_id + 1


def parse_binance_agg_trade(
    raw_payload: str | bytes | Mapping[str, Any],
    *,
    allow_legacy_missing_stream_type: bool = False,
) -> BinanceAggTrade:
    """Parse one current Binance USDⓈ-M aggTrade payload.

    Production callers require ``st=1``. The compatibility switch exists only for
    recorded legacy/test payloads whose transport is independently asserted to be
    an explicitly subscribed USDⓈ-M stream.
    """

    payload = _mapping_payload(raw_payload)
    if "data" in payload and isinstance(payload.get("data"), Mapping):
        payload = payload["data"]
    if payload.get("e") != "aggTrade":
        raise AggTradePayloadError("payload is not an aggTrade event")

    stream_type = _optional_int(payload, "st")
    if stream_type is None:
        if not allow_legacy_missing_stream_type:
            raise AggTradePayloadError("aggTrade payload is missing required UM stream type st=1")
    elif stream_type == CM_STREAM_TYPE:
        raise WrongContractTypeError("COIN-M aggTrade payload st=2 is not valid for CCI UM flow")
    elif stream_type != UM_STREAM_TYPE:
        raise WrongContractTypeError(f"unsupported aggTrade stream type st={stream_type}")

    symbol = str(payload.get("s") or "").strip().upper()
    if not symbol:
        raise AggTradePayloadError("aggTrade symbol is missing")
    price = _decimal(payload, "p", positive=True)
    quantity = _decimal(payload, "q", positive=True)
    normal_quantity = _optional_decimal(payload, "nq")
    if normal_quantity is not None:
        if normal_quantity < 0:
            raise AggTradePayloadError("aggTrade nq must not be negative")
        if normal_quantity > quantity:
            raise AggTradePayloadError("aggTrade nq must not exceed q")

    first_trade_id = _int(payload, "f")
    last_trade_id = _int(payload, "l")
    if last_trade_id < first_trade_id:
        raise AggTradePayloadError("aggTrade last trade ID precedes first trade ID")
    buyer_is_maker = payload.get("m")
    if not isinstance(buyer_is_maker, bool):
        raise AggTradePayloadError("aggTrade m must be a boolean")

    return BinanceAggTrade(
        symbol=symbol,
        aggregate_trade_id=_int(payload, "a"),
        price=price,
        quantity=quantity,
        normal_quantity=normal_quantity,
        first_trade_id=first_trade_id,
        last_trade_id=last_trade_id,
        event_time=_milliseconds_datetime(payload, "E"),
        trade_time=_milliseconds_datetime(payload, "T"),
        buyer_is_maker=buyer_is_maker,
        stream_type=stream_type,
    )


def _mapping_payload(raw_payload: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw_payload, Mapping):
        return raw_payload
    if isinstance(raw_payload, bytes):
        try:
            raw_payload = raw_payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AggTradePayloadError("aggTrade payload is not UTF-8") from exc
    if not isinstance(raw_payload, str):
        raise AggTradePayloadError("aggTrade payload must be JSON text or an object")
    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise AggTradePayloadError("aggTrade payload is malformed JSON") from exc
    if not isinstance(decoded, Mapping):
        raise AggTradePayloadError("aggTrade JSON payload must be an object")
    return decoded


def _decimal(payload: Mapping[str, Any], key: str, *, positive: bool = False) -> Decimal:
    if key not in payload or payload[key] is None or isinstance(payload[key], bool):
        raise AggTradePayloadError(f"aggTrade {key} is missing")
    try:
        value = Decimal(str(payload[key]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AggTradePayloadError(f"aggTrade {key} is not numeric") from exc
    if not value.is_finite() or (positive and value <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise AggTradePayloadError(f"aggTrade {key} must be {qualifier}")
    return value


def _optional_decimal(payload: Mapping[str, Any], key: str) -> Decimal | None:
    if key not in payload or payload[key] is None or payload[key] == "":
        return None
    return _decimal(payload, key)


def _int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        raise AggTradePayloadError(f"aggTrade {key} must be an integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise AggTradePayloadError(f"aggTrade {key} must be an integer") from exc
    if normalized < 0:
        raise AggTradePayloadError(f"aggTrade {key} must not be negative")
    return normalized


def _optional_int(payload: Mapping[str, Any], key: str) -> int | None:
    if key not in payload or payload[key] is None or payload[key] == "":
        return None
    return _int(payload, key)


def _milliseconds_datetime(payload: Mapping[str, Any], key: str) -> datetime:
    value = _int(payload, key)
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError) as exc:
        raise AggTradePayloadError(f"aggTrade {key} is outside the timestamp range") from exc
