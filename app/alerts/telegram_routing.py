from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.data.dtos import NA

logger = logging.getLogger(__name__)


class TelegramDestination(str, Enum):
    SIGNAL_CHANNEL = "signal_channel"
    PUBLIC_CHAT = "public_chat"
    PRIVATE_BOT_CHAT = "private_bot_chat"
    ADMIN_CHAT = "admin_chat"
    UNKNOWN = "unknown"


class TelegramMessageType(str, Enum):
    WOLF_BRIEFING = "wolf_briefing"
    PUBLIC_SIGNAL = "public_signal"
    LIFECYCLE_UPDATE = "lifecycle_update"
    LIMIT_ZONE_HIT = "limit_zone_hit"
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    INVALIDATED = "invalidated"
    WATCHLIST_CONFIRMED = "watchlist_confirmed"
    WELCOME = "welcome"
    ONBOARDING = "onboarding"
    DONATE = "donate"
    ADMIN_REPORT = "admin_report"
    ADMIN_MENU = "admin_menu"
    PUBLIC_MENU = "public_menu"
    DIAGNOSTICS = "diagnostics"
    SCAN_SUMMARY = "scan_summary"
    REJECTED_SETUP = "rejected_setup"
    NO_VALID_SETUP = "no_valid_setup"
    KEYBOARD_MENU = "keyboard_menu"
    UNKNOWN = "unknown"


SIGNAL_CHANNEL_ALLOWED_MESSAGE_TYPES = frozenset(
    {
        TelegramMessageType.WOLF_BRIEFING,
        TelegramMessageType.PUBLIC_SIGNAL,
        TelegramMessageType.LIFECYCLE_UPDATE,
        TelegramMessageType.LIMIT_ZONE_HIT,
        TelegramMessageType.TP_HIT,
        TelegramMessageType.SL_HIT,
        TelegramMessageType.INVALIDATED,
        TelegramMessageType.WATCHLIST_CONFIRMED,
    }
)


@dataclass(frozen=True)
class TelegramRouteDecision:
    allowed: bool
    destination: TelegramDestination
    message_type: TelegramMessageType
    reason: str = NA


class TelegramRoutingError(RuntimeError):
    pass


def can_send_to_destination(
    destination: TelegramDestination | str,
    message_type: TelegramMessageType | str,
) -> TelegramRouteDecision:
    normalized_destination = normalize_destination(destination)
    normalized_message_type = normalize_message_type(message_type)
    if (
        normalized_destination == TelegramDestination.SIGNAL_CHANNEL
        and normalized_message_type not in SIGNAL_CHANNEL_ALLOWED_MESSAGE_TYPES
    ):
        return TelegramRouteDecision(
            False,
            normalized_destination,
            normalized_message_type,
            "message_type_not_allowed_for_signal_channel",
        )
    return TelegramRouteDecision(True, normalized_destination, normalized_message_type)


def assert_signal_channel_allowed(message_type: TelegramMessageType | str) -> None:
    decision = can_send_to_destination(TelegramDestination.SIGNAL_CHANNEL, message_type)
    if not decision.allowed:
        raise TelegramRoutingError(decision.reason)


def log_blocked_telegram_route(decision: TelegramRouteDecision) -> None:
    logger.warning(
        "Telegram send blocked by destination guard: destination_type=%s message_type=%s reason=%s",
        decision.destination.value,
        decision.message_type.value,
        decision.reason,
    )


def normalize_destination(value: TelegramDestination | str | None) -> TelegramDestination:
    if isinstance(value, TelegramDestination):
        return value
    text = _status_key(value)
    for destination in TelegramDestination:
        if text == destination.value:
            return destination
    return TelegramDestination.UNKNOWN


def normalize_message_type(value: TelegramMessageType | str | None) -> TelegramMessageType:
    if isinstance(value, TelegramMessageType):
        return value
    text = _status_key(value)
    for message_type in TelegramMessageType:
        if text == message_type.value:
            return message_type
    return TelegramMessageType.UNKNOWN


def signal_channel_destination_for_chat(
    chat_id: Any,
    *,
    public_channel_id: Any = None,
    wolf_briefing_channel_id: Any = None,
) -> TelegramDestination:
    chat = _display(chat_id)
    if chat != NA and chat in {_display(public_channel_id), _display(wolf_briefing_channel_id)}:
        return TelegramDestination.SIGNAL_CHANNEL
    return TelegramDestination.UNKNOWN


def _status_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _display(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        value = value.value
    if isinstance(value, bool):
        return NA
    text = " ".join(str(value).split())
    return text if text else NA


__all__ = [
    "SIGNAL_CHANNEL_ALLOWED_MESSAGE_TYPES",
    "TelegramDestination",
    "TelegramMessageType",
    "TelegramRouteDecision",
    "TelegramRoutingError",
    "assert_signal_channel_allowed",
    "can_send_to_destination",
    "log_blocked_telegram_route",
    "normalize_destination",
    "normalize_message_type",
    "signal_channel_destination_for_chat",
]
