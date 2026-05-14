from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.data.dtos import NA

CANDLE_CRAFT_SIGNATURE = "Candle Craft | Signal. Structure. Execution."
DEFAULT_RISK_WARNING = (
    "This is not financial advice. Trading involves risk, and every setup can fail at invalidation."
)
TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def format_trade_alert(trade_idea: Any) -> str:
    """Format a structured trade idea into a plain-text alert message."""

    data = _as_mapping(trade_idea)
    symbol = _value_text(data.get("symbol"))

    lines = [
        f"🟢 Trade Setup Alert — {symbol}",
        "",
        f"Direction: {_value_text(data.get('direction'))}",
        f"Exchange: {_value_text(data.get('exchange'))}",
        f"Market type: {_value_text(data.get('market_type'))}",
        f"Timeframe: {_value_text(data.get('timeframe'))}",
        f"Setup type: {_value_text(data.get('setup_type'))}",
        f"Status: {_value_text(data.get('status'))}",
        f"Entry zone: {_format_level(data.get('entry_zone'))}",
        f"Stop loss: {_format_level(data.get('stop_loss'))}",
        f"Invalidation: {_value_text(data.get('invalidation'))}",
        f"Take profits: {_format_take_profits(data.get('take_profits'))}",
        f"Best R:R: {_value_text(data.get('best_rr'))}",
        f"Confidence score: {_value_text(data.get('confidence_score'))}",
        f"Grade: {_value_text(data.get('grade'))}",
        f"Reason for trade: {_value_text(data.get('reason_for_trade'))}",
        f"Confirmed facts: {_format_sequence(data.get('confirmed_facts'))}",
        f"Missing data: {_format_sequence(data.get('missing_data'))}",
        f"Unverified data: {_format_sequence(data.get('unverified_data'))}",
        f"Cancel condition: {_value_text(data.get('cancel_condition'))}",
        f"Risk warning: {_risk_warning(data.get('risk_warning'))}",
        "",
        CANDLE_CRAFT_SIGNATURE,
    ]
    return "\n".join(lines)


def split_message(message: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> tuple[str, ...]:
    """Split long plain-text messages at readable boundaries."""

    if max_length <= 0:
        raise ValueError("max_length must be greater than zero")
    if len(message) <= max_length:
        return (message,)

    chunks: list[str] = []
    remaining = message
    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, max_length + 1)
        if split_at <= 0:
            split_at = max_length

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:max_length]
            split_at = max_length
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    plain_value = _to_plain(value)
    if not isinstance(plain_value, Mapping):
        raise TypeError("trade_idea must be a mapping or a model that can be dumped to a mapping")
    return plain_value


def _to_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return {key: _to_plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_to_plain(item) for item in value)
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


def _format_level(value: Any) -> str:
    plain_value = _to_plain(value)
    if isinstance(plain_value, Mapping):
        price = _value_text(plain_value.get("price"))
        low = _value_text(plain_value.get("low"))
        high = _value_text(plain_value.get("high"))
        if price != NA:
            return price
        if low != NA or high != NA:
            return f"{low} - {high}"
        return NA
    return _value_text(plain_value)


def _format_take_profits(value: Any) -> str:
    plain_value = _to_plain(value)
    if plain_value is None or plain_value == "":
        return NA
    if isinstance(plain_value, str):
        return _value_text(plain_value)
    if not isinstance(plain_value, Sequence):
        return _value_text(plain_value)
    if not plain_value:
        return NA

    targets: list[str] = []
    for index, item in enumerate(plain_value, start=1):
        if isinstance(item, Mapping):
            target_number = _value_text(item.get("target_number", index))
            price = _value_text(item.get("price"))
            targets.append(f"TP{target_number}: {price}")
        else:
            targets.append(f"TP{index}: {_value_text(item)}")
    return "; ".join(targets)


def _format_sequence(value: Any) -> str:
    plain_value = _to_plain(value)
    if plain_value is None or plain_value == "":
        return NA
    if isinstance(plain_value, str):
        return _value_text(plain_value)
    if not isinstance(plain_value, Sequence):
        return _value_text(plain_value)
    cleaned = [_value_text(item) for item in plain_value if _value_text(item) != ""]
    if not cleaned:
        return NA
    return "; ".join(cleaned)


def _risk_warning(value: Any) -> str:
    text = _value_text(value)
    if text == NA:
        return DEFAULT_RISK_WARNING
    return text


def _value_text(value: Any) -> str:
    if value is None:
        return NA
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, str):
        text = value.strip()
        return text if text else NA
    if isinstance(value, bool):
        return str(value)
    return str(value)


__all__ = [
    "CANDLE_CRAFT_SIGNATURE",
    "DEFAULT_RISK_WARNING",
    "TELEGRAM_MAX_MESSAGE_LENGTH",
    "format_trade_alert",
    "split_message",
]
