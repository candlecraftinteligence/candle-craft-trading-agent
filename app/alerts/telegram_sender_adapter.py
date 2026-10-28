"""Opt-in P0B3B1 transport bridge; no production orchestration imports this module.

Stage 1 remains the authority for canonical reservations, epochs and retry admission.
This adapter sends exactly one already-reserved part, never splits or retries, and
does not treat a sender's summary status as provider acknowledgment evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from datetime import datetime
from typing import Any

from app.alerts.telegram_outbox import FAILED_FINAL, RETRYABLE, SENT, UNCERTAIN
from app.alerts.telegram_outbox_protocol import TelegramTransportOutcome, TelegramTransportRequest
from app.alerts.telegram_routing import (
    TelegramDestination,
    TelegramMessageType,
    can_send_to_destination,
    normalize_message_type,
)
from app.alerts.telegram_sender import PublicWatchlistSendGuard, TelegramSender, TelegramSendResult
from app.alerts.templates import TELEGRAM_MAX_MESSAGE_LENGTH
from app.data.dtos import NA


def _outcome(state: str, category: str, *, http_status: int | None = None) -> TelegramTransportOutcome:
    # Never echo provider text, exception messages, payloads, or credential-bearing URLs.
    return TelegramTransportOutcome(
        delivery_state=state,
        http_status=http_status,
        error_category=category,
        error=category,
    )


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _absent(value: Any) -> bool:
    return value is None or (type(value) is str and value in ("", NA))


def _valid_request(request: TelegramTransportRequest) -> bool:
    if not isinstance(request, TelegramTransportRequest):
        return False
    if not all(_positive_int(value) for value in (
        request.event_id, request.reservation_id, request.part_id, request.part_index, request.part_count,
    )) or request.part_index > request.part_count:
        return False
    if not all(type(value) is str and value.strip() and value != NA for value in (
        request.event_key, request.attempt_id, request.message_hash, request.destination_chat_id,
    )):
        return False
    if request.destination_kind not in (
        TelegramDestination.PUBLIC_CHAT.value, TelegramDestination.SIGNAL_CHANNEL.value,
    ):
        return False
    text = request.payload_text
    if type(text) is not str or not text.strip() or len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
        return False
    try:
        return request.payload_hash == hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return False


def _valid_sent_at(value: Any) -> bool:
    if value is None:
        return True  # Optional provider metadata; never invent a provider timestamp.
    if type(value) is not str:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def adapt_telegram_send_result(
    result: TelegramSendResult,
    *,
    request: TelegramTransportRequest,
) -> TelegramTransportOutcome:
    """Translate ONE send_part result, failing closed on missing/contradictory evidence.

    Input evidence is the existing sender's flattened provider record, not an HTTP
    response or independent attestation. A skipped result alone cannot establish
    non-transmission: only the callable's observed preflight can establish that.
    """
    if not _valid_request(request) or not isinstance(result, TelegramSendResult):
        return _outcome(UNCERTAIN, "adapter_invalid_result_or_request")
    if type(result.telegram_results) is not tuple or len(result.telegram_results) != 1:
        return _outcome(UNCERTAIN, "adapter_expected_one_part_result")
    raw = result.telegram_results[0]
    if type(raw) is not dict:
        return _outcome(UNCERTAIN, "adapter_invalid_provider_record")
    if (
        not _positive_int(raw.get("part_number"))
        or not _positive_int(raw.get("total_parts"))
        or raw["part_number"] != request.part_index
        or raw["total_parts"] != request.part_count
    ):
        return _outcome(UNCERTAIN, "adapter_part_identity_mismatch")
    http_status = raw.get("http_status")
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        return _outcome(UNCERTAIN, "adapter_invalid_http_status")
    state = raw.get("delivery_state")
    if result.delivery_state != state or result.status != raw.get("status"):
        return _outcome(UNCERTAIN, "adapter_sender_provider_state_mismatch", http_status=http_status)
    if state == SENT and result.status == "sent":
        if (
            http_status != 200
            or not _positive_int(raw.get("message_id"))
            or type(raw.get("chat_id")) is not int
            or raw["chat_id"] == 0
            or str(raw["chat_id"]) != request.destination_chat_id
            or not _absent(result.error_message)
            or not _absent(raw.get("error"))
            or not _absent(raw.get("error_category"))
            or raw.get("rate_limited") is not False
            or raw.get("retry_after") is not None
            or not _valid_sent_at(raw.get("sent_at"))
        ):
            return _outcome(UNCERTAIN, "adapter_unverified_success", http_status=http_status)
        return TelegramTransportOutcome(
            delivery_state=SENT,
            http_status=http_status,
            telegram_message_id=str(raw["message_id"]),
            telegram_chat_id=str(raw["chat_id"]),
            sent_at=raw.get("sent_at"),
        )

    # A failure carrying acknowledgment metadata may have been accepted. Never retry it.
    if result.status != "failed" or any(not _absent(raw.get(key)) for key in ("message_id", "chat_id", "sent_at")):
        return _outcome(UNCERTAIN, "adapter_contradictory_failure", http_status=http_status)
    category = raw.get("error_category")
    if type(http_status) is int and 500 <= http_status <= 599:
        return _outcome(UNCERTAIN, "http_5xx_uncertain_acceptance", http_status=http_status)
    if (
        type(raw.get("error")) is not str or _absent(raw["error"])
        or result.error_message != raw["error"]
    ):
        return _outcome(UNCERTAIN, "adapter_inconsistent_failure_evidence", http_status=http_status)
    if (
        state == RETRYABLE
        and http_status is None
        and category == "connect_failure_before_transmission"
        and raw.get("rate_limited") is False
        and raw.get("retry_after") is None
    ):
        return _outcome(RETRYABLE, category)
    if (
        state == RETRYABLE and http_status == 429 and category == "telegram_rate_limited"
        and raw.get("rate_limited") is True
    ):
        delay = raw.get("retry_after")
        if delay is None or (type(delay) in (int, float) and math.isfinite(delay) and delay >= 0):
            return TelegramTransportOutcome(
                delivery_state=RETRYABLE, http_status=429, rate_limited=True,
                retry_after=delay, error_category=category, error=category,
            )
    if (
        state == FAILED_FINAL and type(http_status) is int and 400 <= http_status < 500
        and http_status not in (408, 429) and category == "telegram_permanent_rejection"
        and raw.get("rate_limited") is False and raw.get("retry_after") is None
    ):
        return _outcome(FAILED_FINAL, category, http_status=http_status)
    # In particular, telegram_api_rejection:* rewrites http_status from the body:
    # it is not verified wire HTTP evidence. Malformed HTTP-200 bodies also land here.
    return _outcome(UNCERTAIN, "adapter_unconfirmed_transmission", http_status=http_status)


class TelegramSenderTransport:
    """Explicitly injected Stage-2 transport; requires a Stage-1 authorized request.

    This is not a standalone authorization or deduplication service. Repeated calls
    must be admitted by the existing durable protocol. No production wiring occurs.
    """

    def __init__(self, sender: TelegramSender, *, message_type: TelegramMessageType | str) -> None:
        self._sender = sender
        self._message_type = normalize_message_type(message_type)

    async def __call__(self, request: TelegramTransportRequest) -> TelegramTransportOutcome:
        if not _valid_request(request):
            return _outcome(FAILED_FINAL, "adapter_preflight_invalid_request")
        if (
            request.destination_chat_id != self._sender.destination_identity
            or request.destination_kind != self._sender.destination_kind
            or self._message_type == TelegramMessageType.UNKNOWN
            or not can_send_to_destination(request.destination_kind, self._message_type).allowed
        ):
            return _outcome(FAILED_FINAL, "adapter_preflight_destination_or_route_mismatch")
        guard = PublicWatchlistSendGuard(
            event_key=request.event_key, reservation_id=request.reservation_id,
            event_id=request.event_id, attempt_id=request.attempt_id,
        )
        blocked = self._sender.preflight(message_type=self._message_type, public_watchlist_guard=guard)
        if blocked is not None:
            # The protocol recognizes only four states. Preserve the local reason,
            # using terminal FAILED_FINAL rather than inventing SENT or retry eligibility.
            if (
                isinstance(blocked, TelegramSendResult) and blocked.status == "skipped"
                and blocked.telegram_results == ()
                and blocked.delivery_state in {"POLICY_DISABLED", "SKIPPED_DRY_RUN", FAILED_FINAL}
                and blocked.error_message in {
                    "local_manual_mode_disabled", "telegram_sending_disabled", "telegram_dry_run_enabled",
                    "missing_telegram_credentials", "public_watchlist_missing_required_reservation",
                    "message_type_not_allowed_for_signal_channel",
                }
            ):
                return _outcome(FAILED_FINAL, f"sender_preflight:{blocked.error_message}")
            return _outcome(UNCERTAIN, "adapter_invalid_preflight_result")
        try:
            result = await self._sender.send_part(
                request.payload_text, part_number=request.part_index, total_parts=request.part_count,
                message_type=self._message_type, public_watchlist_guard=guard,
            )
        except asyncio.CancelledError:
            raise  # The protocol owns cancellation fencing and durable uncertainty.
        except Exception:
            return _outcome(UNCERTAIN, "adapter_sender_exception_after_invocation")
        return adapt_telegram_send_result(result, request=request)
