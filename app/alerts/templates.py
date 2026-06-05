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

    from app.formatters.telegram_signal_formatter import (
        format_premium_public_signal_message,
        format_public_no_trade_message,
    )

    data = _as_mapping(trade_idea)
    message = _signal_message_from_trade_idea(data)
    if _is_rejected_trade_idea(data):
        return format_public_no_trade_message(message, _rejection_reason(data))
    return format_premium_public_signal_message(message)


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


def _signal_message_from_trade_idea(data: Mapping[str, Any]) -> Any:
    from app.formatters.telegram_signal_formatter import TelegramSignalMessage

    return TelegramSignalMessage(
        symbol=data.get("symbol", NA),
        direction=data.get("direction", NA),
        mode=data.get("setup_type", NA),
        quality=data.get("grade", NA),
        entry_low=_level_field(data.get("entry_zone"), "low"),
        entry_high=_level_field(data.get("entry_zone"), "high"),
        stop_loss=_level_field(data.get("stop_loss"), "price"),
        tp1=_take_profit_price(data.get("take_profits"), 1),
        tp2=_take_profit_price(data.get("take_profits"), 2),
        tp3=_take_profit_price(data.get("take_profits"), 3),
        planned_rr=data.get("best_rr", NA),
        structure_reason=_first_value(
            data.get("reason_for_trade", NA),
            _format_sequence(data.get("confirmed_facts")),
        ),
        invalidation_reason=data.get("invalidation", NA),
    )


def _is_rejected_trade_idea(data: Mapping[str, Any]) -> bool:
    if _value_text(data.get("status")).lower() == "rejected":
        return True
    gate = _to_plain(data.get("quality_gate_result"))
    if isinstance(gate, Mapping) and gate.get("passed") is False:
        return True
    return False


def _rejection_reason(data: Mapping[str, Any]) -> str:
    gate = _to_plain(data.get("quality_gate_result"))
    if isinstance(gate, Mapping):
        violations = gate.get("violations")
        if isinstance(violations, Sequence) and not isinstance(violations, (str, bytes)):
            for violation in violations:
                plain = _to_plain(violation)
                if isinstance(plain, Mapping):
                    text = _value_text(plain.get("message"))
                    if text != NA:
                        return text
    return _value_text(data.get("reason_for_trade"))


def _level_field(value: Any, field: str) -> Any:
    plain_value = _to_plain(value)
    if isinstance(plain_value, Mapping):
        return plain_value.get(field, NA)
    return NA


def _take_profit_price(value: Any, target_number: int) -> Any:
    plain_value = _to_plain(value)
    if not isinstance(plain_value, Sequence) or isinstance(plain_value, (str, bytes)):
        return NA
    index = target_number - 1
    if index >= len(plain_value):
        return NA
    target = plain_value[index]
    if isinstance(target, Mapping):
        return target.get("price", NA)
    return target


def _first_value(*values: Any) -> Any:
    for value in values:
        if _value_text(value) != NA:
            return value
    return NA


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
