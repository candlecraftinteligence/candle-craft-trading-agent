from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


UM_STREAM_TYPE = 1
CM_STREAM_TYPE = 2


class LiquidationPayloadError(ValueError):
    """Raised when a Binance force-order payload cannot be trusted."""


class WrongLiquidationContractTypeError(LiquidationPayloadError):
    """Raised when a detectable non-USDⓈ-M force order reaches this parser."""


class LiquidatedPositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class BinanceLiquidationOrder(BaseModel):
    """One normalized observation from Binance's force-order snapshot stream."""

    symbol: str
    pair_symbol: str
    order_side: str
    liquidated_position_side: LiquidatedPositionSide
    order_type: str
    time_in_force: str
    original_quantity: Decimal
    order_price: Decimal
    average_price: Decimal
    order_status: str
    last_filled_quantity: Decimal
    accumulated_filled_quantity: Decimal
    event_time: datetime
    trade_time: datetime
    stream_type: int

    model_config = ConfigDict(frozen=True)

    @property
    def quote_notional(self) -> Decimal:
        """Executed quote notional: accumulated filled quantity × average fill price."""

        return self.accumulated_filled_quantity * self.average_price


def parse_binance_liquidation(
    raw_payload: str | bytes | Mapping[str, Any],
) -> BinanceLiquidationOrder:
    """Parse the current merged Binance Futures ``forceOrder`` payload.

    Binance now publishes a merged UM/CM all-market stream. CCI is USDⓈ-M only,
    so the post-migration ``st=1`` discriminator is mandatory and ``st=2`` is
    rejected. The current contract does not expose a liquidation order ID.
    """

    payload = _mapping_payload(raw_payload)
    if "data" in payload and isinstance(payload.get("data"), Mapping):
        payload = payload["data"]
    if payload.get("e") != "forceOrder":
        raise LiquidationPayloadError("payload is not a forceOrder event")

    stream_type = _int(payload, "st")
    if stream_type == CM_STREAM_TYPE:
        raise WrongLiquidationContractTypeError(
            "COIN-M forceOrder payload st=2 is not valid for CCI USDⓈ-M liquidation flow"
        )
    if stream_type != UM_STREAM_TYPE:
        raise WrongLiquidationContractTypeError(
            f"unsupported forceOrder stream type st={stream_type}"
        )

    order = payload.get("o")
    if not isinstance(order, Mapping):
        raise LiquidationPayloadError("forceOrder order object o is missing")

    symbol = _text(order, "s").upper()
    pair_symbol = _text(payload, "ps").upper()
    order_side = _text(order, "S").upper()
    if order_side == "SELL":
        liquidated_side = LiquidatedPositionSide.LONG
    elif order_side == "BUY":
        liquidated_side = LiquidatedPositionSide.SHORT
    else:
        raise LiquidationPayloadError("forceOrder S must be BUY or SELL")

    original_quantity = _decimal(order, "q", positive=True)
    order_price = _decimal(order, "p", non_negative=True)
    average_price = _decimal(order, "ap", positive=True)
    last_filled_quantity = _decimal(order, "l", non_negative=True)
    accumulated_filled_quantity = _decimal(order, "z", positive=True)
    if accumulated_filled_quantity > original_quantity:
        raise LiquidationPayloadError("forceOrder accumulated fill z exceeds original q")
    if last_filled_quantity > accumulated_filled_quantity:
        raise LiquidationPayloadError("forceOrder last fill l exceeds accumulated fill z")

    return BinanceLiquidationOrder(
        symbol=symbol,
        pair_symbol=pair_symbol,
        order_side=order_side,
        liquidated_position_side=liquidated_side,
        order_type=_text(order, "o").upper(),
        time_in_force=_text(order, "f").upper(),
        original_quantity=original_quantity,
        order_price=order_price,
        average_price=average_price,
        order_status=_text(order, "X").upper(),
        last_filled_quantity=last_filled_quantity,
        accumulated_filled_quantity=accumulated_filled_quantity,
        event_time=_milliseconds_datetime(payload, "E"),
        trade_time=_milliseconds_datetime(order, "T"),
        stream_type=stream_type,
    )


def _mapping_payload(raw_payload: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw_payload, Mapping):
        return raw_payload
    if isinstance(raw_payload, bytes):
        try:
            raw_payload = raw_payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LiquidationPayloadError("forceOrder payload is not UTF-8") from exc
    if not isinstance(raw_payload, str):
        raise LiquidationPayloadError("forceOrder payload must be JSON text or an object")
    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise LiquidationPayloadError("forceOrder payload is malformed JSON") from exc
    if not isinstance(decoded, Mapping):
        raise LiquidationPayloadError("forceOrder JSON payload must be an object")
    return decoded


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    normalized = str(value).strip() if value is not None else ""
    if not normalized:
        raise LiquidationPayloadError(f"forceOrder {key} is missing")
    return normalized


def _decimal(
    payload: Mapping[str, Any],
    key: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    value = payload.get(key)
    if value is None or value == "" or isinstance(value, bool):
        raise LiquidationPayloadError(f"forceOrder {key} is missing")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiquidationPayloadError(f"forceOrder {key} is not numeric") from exc
    if not normalized.is_finite():
        raise LiquidationPayloadError(f"forceOrder {key} must be finite")
    if positive and normalized <= 0:
        raise LiquidationPayloadError(f"forceOrder {key} must be positive")
    if non_negative and normalized < 0:
        raise LiquidationPayloadError(f"forceOrder {key} must not be negative")
    return normalized


def _int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        raise LiquidationPayloadError(f"forceOrder {key} must be an integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise LiquidationPayloadError(f"forceOrder {key} must be an integer") from exc
    if normalized < 0:
        raise LiquidationPayloadError(f"forceOrder {key} must not be negative")
    return normalized


def _milliseconds_datetime(payload: Mapping[str, Any], key: str) -> datetime:
    value = _int(payload, key)
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError) as exc:
        raise LiquidationPayloadError(
            f"forceOrder {key} is outside the timestamp range"
        ) from exc
