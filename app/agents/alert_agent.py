from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from app.alerts.telegram import TELEGRAM_API_BASE_URL, send_telegram_messages
from app.alerts.templates import TELEGRAM_MAX_MESSAGE_LENGTH, format_trade_alert, split_message

logger = logging.getLogger(__name__)


class AlertChannel(str, Enum):
    TELEGRAM = "telegram"
    CONSOLE = "console"
    WEBHOOK = "webhook"


class AlertStatus(str, Enum):
    DRY_RUN = "dry_run"
    SENT = "sent"
    FAILED = "failed"


class AlertInput(BaseModel):
    trade_idea: Any
    channel: AlertChannel = AlertChannel.CONSOLE
    dry_run: bool = True
    deduplication_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class AlertDeliveryResult(BaseModel):
    channel: AlertChannel
    status: AlertStatus
    detail: str
    part_number: int | None = None
    total_parts: int | None = None
    http_status: int | None = None
    rate_limited: bool = False
    error: str | None = None

    model_config = ConfigDict(frozen=True)


class AlertResult(BaseModel):
    status: AlertStatus
    channel: AlertChannel
    dry_run: bool
    formatted_message: str
    message_parts: tuple[str, ...]
    delivery_results: tuple[AlertDeliveryResult, ...]
    deduplication_key: str | None = None
    deduplication_marked: bool = False

    model_config = ConfigDict(frozen=True)


class AlertAgent:
    """Format and optionally deliver trade idea alerts.

    The default path is dry-run formatting only. The agent does not call
    exchanges, use private API access, place orders, or execute trades.
    """

    def __init__(
        self,
        *,
        telegram_http_client: httpx.AsyncClient | None = None,
        telegram_api_base_url: str = TELEGRAM_API_BASE_URL,
        telegram_timeout: float = 10.0,
        max_message_length: int = TELEGRAM_MAX_MESSAGE_LENGTH,
    ) -> None:
        self._telegram_http_client = telegram_http_client
        self._telegram_api_base_url = telegram_api_base_url
        self._telegram_timeout = telegram_timeout
        self._max_message_length = max_message_length

    def format(self, trade_idea: Any) -> str:
        return format_trade_alert(trade_idea)

    async def send(
        self,
        alert: AlertInput | Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> AlertResult:
        alert_input = _normalize_input(alert, overrides)
        formatted_message = format_trade_alert(alert_input.trade_idea)
        message_parts = split_message(formatted_message, self._max_message_length)
        deduplication_marked = alert_input.deduplication_key is not None

        if alert_input.dry_run:
            logger.info(
                "Dry-run alert for channel=%s deduplication_key=%s would send: %s",
                alert_input.channel.value,
                alert_input.deduplication_key,
                formatted_message,
            )
            delivery = AlertDeliveryResult(
                channel=alert_input.channel,
                status=AlertStatus.DRY_RUN,
                detail="Dry run: no alert was sent.",
                total_parts=len(message_parts),
            )
            return AlertResult(
                status=AlertStatus.DRY_RUN,
                channel=alert_input.channel,
                dry_run=True,
                formatted_message=formatted_message,
                message_parts=message_parts,
                delivery_results=(delivery,),
                deduplication_key=alert_input.deduplication_key,
                deduplication_marked=deduplication_marked,
            )

        if alert_input.channel == AlertChannel.TELEGRAM:
            return await self._send_telegram(
                alert_input=alert_input,
                formatted_message=formatted_message,
                message_parts=message_parts,
                deduplication_marked=deduplication_marked,
            )

        if alert_input.channel == AlertChannel.CONSOLE:
            logger.info("Console alert: %s", formatted_message)
            delivery = AlertDeliveryResult(
                channel=AlertChannel.CONSOLE,
                status=AlertStatus.SENT,
                detail="Console alert logged.",
                total_parts=len(message_parts),
            )
            return AlertResult(
                status=AlertStatus.SENT,
                channel=AlertChannel.CONSOLE,
                dry_run=False,
                formatted_message=formatted_message,
                message_parts=message_parts,
                delivery_results=(delivery,),
                deduplication_key=alert_input.deduplication_key,
                deduplication_marked=deduplication_marked,
            )

        delivery = AlertDeliveryResult(
            channel=alert_input.channel,
            status=AlertStatus.FAILED,
            detail="Webhook delivery is not implemented in Phase 8.",
            error="Unsupported alert channel for live send.",
            total_parts=len(message_parts),
        )
        return AlertResult(
            status=AlertStatus.FAILED,
            channel=alert_input.channel,
            dry_run=False,
            formatted_message=formatted_message,
            message_parts=message_parts,
            delivery_results=(delivery,),
            deduplication_key=alert_input.deduplication_key,
            deduplication_marked=deduplication_marked,
        )

    async def analyze(
        self,
        alert: AlertInput | Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> AlertResult:
        return await self.send(alert, **overrides)

    async def _send_telegram(
        self,
        *,
        alert_input: AlertInput,
        formatted_message: str,
        message_parts: tuple[str, ...],
        deduplication_marked: bool,
    ) -> AlertResult:
        if not alert_input.telegram_bot_token:
            return self._failed_result(
                alert_input=alert_input,
                formatted_message=formatted_message,
                message_parts=message_parts,
                detail="Missing telegram_bot_token for live Telegram send.",
                error="telegram_bot_token is required when dry_run=False and channel=telegram.",
                deduplication_marked=deduplication_marked,
            )
        if not alert_input.telegram_chat_id:
            return self._failed_result(
                alert_input=alert_input,
                formatted_message=formatted_message,
                message_parts=message_parts,
                detail="Missing telegram_chat_id for live Telegram send.",
                error="telegram_chat_id is required when dry_run=False and channel=telegram.",
                deduplication_marked=deduplication_marked,
            )

        raw_results = await send_telegram_messages(
            bot_token=alert_input.telegram_bot_token,
            chat_id=alert_input.telegram_chat_id,
            message=formatted_message,
            http_client=self._telegram_http_client,
            api_base_url=self._telegram_api_base_url,
            timeout=self._telegram_timeout,
            max_message_length=self._max_message_length,
        )
        delivery_results = tuple(
            AlertDeliveryResult(
                channel=AlertChannel.TELEGRAM,
                status=AlertStatus(raw_result["status"]),
                detail=_delivery_detail(raw_result),
                part_number=raw_result.get("part_number"),
                total_parts=raw_result.get("total_parts"),
                http_status=raw_result.get("http_status"),
                rate_limited=raw_result.get("rate_limited", False),
                error=raw_result.get("error"),
            )
            for raw_result in raw_results
        )
        status = (
            AlertStatus.SENT
            if delivery_results and all(result.status == AlertStatus.SENT for result in delivery_results)
            else AlertStatus.FAILED
        )

        return AlertResult(
            status=status,
            channel=AlertChannel.TELEGRAM,
            dry_run=False,
            formatted_message=formatted_message,
            message_parts=message_parts,
            delivery_results=delivery_results,
            deduplication_key=alert_input.deduplication_key,
            deduplication_marked=deduplication_marked,
        )

    def _failed_result(
        self,
        *,
        alert_input: AlertInput,
        formatted_message: str,
        message_parts: tuple[str, ...],
        detail: str,
        error: str,
        deduplication_marked: bool,
    ) -> AlertResult:
        delivery = AlertDeliveryResult(
            channel=alert_input.channel,
            status=AlertStatus.FAILED,
            detail=detail,
            error=error,
            total_parts=len(message_parts),
        )
        return AlertResult(
            status=AlertStatus.FAILED,
            channel=alert_input.channel,
            dry_run=False,
            formatted_message=formatted_message,
            message_parts=message_parts,
            delivery_results=(delivery,),
            deduplication_key=alert_input.deduplication_key,
            deduplication_marked=deduplication_marked,
        )


async def send_alert(
    alert: AlertInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> AlertResult:
    return await AlertAgent().send(alert, **overrides)


def _normalize_input(
    alert: AlertInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> AlertInput:
    if alert is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(alert, AlertInput):
        raw = alert.model_dump()
        raw.update(overrides)
    else:
        raw = dict(alert)
        raw.update(overrides)
    return AlertInput.model_validate(raw)


def _delivery_detail(raw_result: Mapping[str, Any]) -> str:
    if raw_result.get("status") == AlertStatus.SENT.value:
        part_number = raw_result.get("part_number")
        total_parts = raw_result.get("total_parts")
        return f"Telegram message part {part_number} of {total_parts} sent."
    error = raw_result.get("error")
    return str(error) if error else "Telegram delivery failed."


__all__ = [
    "AlertAgent",
    "AlertChannel",
    "AlertDeliveryResult",
    "AlertInput",
    "AlertResult",
    "AlertStatus",
    "send_alert",
]
