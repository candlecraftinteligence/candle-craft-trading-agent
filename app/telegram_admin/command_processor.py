from __future__ import annotations

import json
import mimetypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.alerts.telegram import DEFAULT_TELEGRAM_TIMEOUT, TELEGRAM_API_BASE_URL, send_telegram_messages
from app.alerts.templates import TELEGRAM_MAX_MESSAGE_LENGTH, split_message
from app.data.dtos import NA
from app.telegram_admin.client import TelegramAdminConfig
from app.telegram_admin.commands import (
    AdminCommandResponse,
    TelegramAdminCommandService,
    WOLF_BRIEFING_PUBLISH_COMMAND,
    command_for_callback_data,
    normalize_admin_command,
    reply_keyboard_remove_markup,
)

DEFAULT_ADMIN_COMMANDS_DIR = Path("scan_runs") / "admin_commands"
DEFAULT_ADMIN_COMMAND_STATE_PATH = DEFAULT_ADMIN_COMMANDS_DIR / "state.json"
DEFAULT_ADMIN_COMMAND_AUDIT_PATH = DEFAULT_ADMIN_COMMANDS_DIR / "commands.jsonl"
DEFAULT_GET_UPDATES_TIMEOUT_SECONDS = 0
DEFAULT_COMMAND_LIMIT = 10
REPLY_KEYBOARD_CLEANUP_MESSAGE = "Candle Craft controls now appear inside messages."
REPLY_KEYBOARD_CLEANUP_STATE_KEY = "reply_keyboard_removed_chat_hashes"

ADMIN_COMMAND_DELIVERY_STATUSES: tuple[str, ...] = (
    "dry_run",
    "sent_admin",
    "sent_public",
    "skipped_disabled",
    "skipped_missing_credentials",
    "ignored_unauthorized",
    "failed",
)


class TelegramAdminCommandTransport(Protocol):
    async def get_updates(
        self,
        *,
        bot_token: str,
        offset: int | None,
        limit: int,
        timeout: int,
    ) -> tuple[Mapping[str, Any], ...]:
        """Read Telegram updates for the admin bot."""

    async def send_message(
        self,
        *,
        bot_token: str,
        chat_id: str,
        message: str,
        reply_markup: Mapping[str, Any] | None = None,
        photo_path: Path | None = None,
        photo_url: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Send one admin command response through Telegram."""

    async def answer_callback_query(
        self,
        *,
        bot_token: str,
        callback_query_id: str,
        text: str | None = None,
    ) -> Mapping[str, Any]:
        """Acknowledge a Telegram inline button callback."""


class HttpxTelegramAdminCommandTransport:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = TELEGRAM_API_BASE_URL,
        timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
    ) -> None:
        self._http_client = http_client
        self._api_base_url = api_base_url
        self._timeout = timeout

    async def get_updates(
        self,
        *,
        bot_token: str,
        offset: int | None,
        limit: int,
        timeout: int,
    ) -> tuple[Mapping[str, Any], ...]:
        params: dict[str, Any] = {"limit": limit, "timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        endpoint = f"{self._api_base_url.rstrip('/')}/bot{bot_token}/getUpdates"
        if self._http_client is not None:
            response = await self._http_client.get(endpoint, params=params, timeout=self._timeout)
            return _updates_from_response(response)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(endpoint, params=params)
            return _updates_from_response(response)

    async def send_message(
        self,
        *,
        bot_token: str,
        chat_id: str,
        message: str,
        reply_markup: Mapping[str, Any] | None = None,
        photo_path: Path | None = None,
        photo_url: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        if photo_path is not None:
            return await _send_admin_photo_file_with_reply_markup(
                bot_token=bot_token,
                chat_id=chat_id,
                message=message,
                photo_path=photo_path,
                reply_markup=reply_markup,
                http_client=self._http_client,
                api_base_url=self._api_base_url,
                timeout=self._timeout,
            )
        if photo_url is not None:
            return await _send_admin_photo_with_reply_markup(
                bot_token=bot_token,
                chat_id=chat_id,
                message=message,
                photo_url=photo_url,
                reply_markup=reply_markup,
                http_client=self._http_client,
                api_base_url=self._api_base_url,
                timeout=self._timeout,
            )
        if reply_markup is not None:
            return await _send_admin_messages_with_reply_markup(
                bot_token=bot_token,
                chat_id=chat_id,
                message=message,
                reply_markup=reply_markup,
                http_client=self._http_client,
                api_base_url=self._api_base_url,
                timeout=self._timeout,
            )
        return await send_telegram_messages(
            bot_token=bot_token,
            chat_id=chat_id,
            message=message,
            http_client=self._http_client,
            api_base_url=self._api_base_url,
            timeout=self._timeout,
        )

    async def answer_callback_query(
        self,
        *,
        bot_token: str,
        callback_query_id: str,
        text: str | None = None,
    ) -> Mapping[str, Any]:
        return await _answer_callback_query(
            bot_token=bot_token,
            callback_query_id=callback_query_id,
            text=text,
            http_client=self._http_client,
            api_base_url=self._api_base_url,
            timeout=self._timeout,
        )


@dataclass(frozen=True)
class AdminCommandProcessingResult:
    delivery_status: str
    updates_seen: int
    processed_count: int
    sent_count: int
    audit_path: Path
    state_path: Path
    previews: tuple[str, ...] = ()
    error_message: str = NA

    @property
    def failed(self) -> bool:
        return self.delivery_status == "failed"


@dataclass(frozen=True)
class _ProcessedUpdate:
    delivery_status: str
    sent: bool = False
    preview: str = NA
    error_message: str = NA


async def process_telegram_admin_commands(
    *,
    config: TelegramAdminConfig,
    command_service: TelegramAdminCommandService,
    transport: TelegramAdminCommandTransport | None = None,
    state_path: Path | str = DEFAULT_ADMIN_COMMAND_STATE_PATH,
    audit_path: Path | str = DEFAULT_ADMIN_COMMAND_AUDIT_PATH,
    limit: int = DEFAULT_COMMAND_LIMIT,
    dry_run: bool = False,
    show_preview: bool = False,
    updates: Sequence[Mapping[str, Any]] | None = None,
    get_updates_timeout: int = DEFAULT_GET_UPDATES_TIMEOUT_SECONDS,
) -> AdminCommandProcessingResult:
    effective_config = replace(config, dry_run=True) if dry_run else config
    state = Path(state_path)
    audit = Path(audit_path)
    latest_processed_update_id = _load_latest_processed_update_id(state)
    safe_limit = max(1, int(limit))
    admin_transport = transport or HttpxTelegramAdminCommandTransport(timeout=effective_config.timeout)

    if updates is None:
        skipped = _network_skip_status(effective_config)
        if skipped is not None:
            return AdminCommandProcessingResult(
                delivery_status=skipped,
                updates_seen=0,
                processed_count=0,
                sent_count=0,
                audit_path=audit,
                state_path=state,
                previews=_preview_tuple(show_preview, skipped),
            )
        try:
            updates = await admin_transport.get_updates(
                bot_token=effective_config.bot_token or "",
                offset=latest_processed_update_id + 1 if latest_processed_update_id is not None else None,
                limit=safe_limit,
                timeout=get_updates_timeout,
            )
        except Exception as exc:
            return AdminCommandProcessingResult(
                delivery_status="failed",
                updates_seen=0,
                processed_count=0,
                sent_count=0,
                audit_path=audit,
                state_path=state,
                error_message=_sanitize_error(exc, effective_config),
            )

    selected_updates = tuple(_limited_new_updates(updates, latest_processed_update_id, safe_limit))
    processed: list[_ProcessedUpdate] = []
    for update in selected_updates:
        result = await _process_update(
            update,
            config=effective_config,
            command_service=command_service,
            transport=admin_transport,
            state_path=state,
            audit_path=audit,
        )
        processed.append(result)

    if not processed:
        return AdminCommandProcessingResult(
            delivery_status="dry_run" if effective_config.dry_run else "sent_admin",
            updates_seen=len(tuple(updates)),
            processed_count=0,
            sent_count=0,
            audit_path=audit,
            state_path=state,
            previews=_preview_tuple(show_preview, "No new admin command updates."),
        )

    failed = next((item for item in processed if item.delivery_status == "failed"), None)
    status = failed.delivery_status if failed is not None else processed[-1].delivery_status
    return AdminCommandProcessingResult(
        delivery_status=status,
        updates_seen=len(tuple(updates)),
        processed_count=len(processed),
        sent_count=sum(1 for item in processed if item.sent),
        audit_path=audit,
        state_path=state,
        previews=tuple(item.preview for item in processed if show_preview and item.preview != NA),
        error_message=failed.error_message if failed is not None else NA,
    )


async def _process_update(
    update: Mapping[str, Any],
    *,
    config: TelegramAdminConfig,
    command_service: TelegramAdminCommandService,
    transport: TelegramAdminCommandTransport,
    state_path: Path,
    audit_path: Path,
) -> _ProcessedUpdate:
    update_id = _update_id(update)
    if update_id is None:
        return _ProcessedUpdate("ignored_unauthorized", preview="Ignored update without update_id.")

    callback_query = _callback_query_payload(update)
    if callback_query:
        await _answer_callback_query_safely(
            config=config,
            callback_query_id=_callback_query_id(callback_query),
            transport=transport,
        )
        chat_id = _callback_chat_id(callback_query)
        chat_type = _callback_chat_type(callback_query)
        if not _command_chat_is_private(chat_id, chat_type, config):
            _save_latest_processed_update_id(state_path, update_id)
            return _ProcessedUpdate("ignored_unauthorized", preview="Ignored non-private Telegram command update.")
        callback_scope, command = command_for_callback_data(_callback_data(callback_query))
        is_admin = _chat_matches_admin(chat_id, config.admin_chat_id)
        if callback_scope == "public":
            return await _process_public_update(
                update_id,
                command,
                chat_id,
                config=config,
                command_service=command_service,
                transport=transport,
                state_path=state_path,
                audit_path=audit_path,
            )
        if callback_scope == "admin" and not is_admin:
            return await _process_public_update(
                update_id,
                "/status",
                chat_id,
                config=config,
                command_service=command_service,
                transport=transport,
                state_path=state_path,
                audit_path=audit_path,
            )
        if callback_scope == "admin" and command == WOLF_BRIEFING_PUBLISH_COMMAND:
            return await _process_admin_wolf_publish_update(
                update_id,
                chat_id,
                config=config,
                command_service=command_service,
                transport=transport,
                state_path=state_path,
                audit_path=audit_path,
            )
        if callback_scope == "admin":
            return await _process_admin_update(
                update_id,
                command,
                chat_id,
                config=config,
                command_service=command_service,
                transport=transport,
                state_path=state_path,
                audit_path=audit_path,
            )
        if is_admin:
            return await _process_admin_update(
                update_id,
                command,
                chat_id,
                config=config,
                command_service=command_service,
                transport=transport,
                state_path=state_path,
                audit_path=audit_path,
            )
        return await _process_public_update(
            update_id,
            command,
            chat_id,
            config=config,
            command_service=command_service,
            transport=transport,
            state_path=state_path,
            audit_path=audit_path,
        )

    message = _message_payload(update)
    chat_id = _chat_id(message)
    chat_type = _chat_type(message)
    if not _command_chat_is_private(chat_id, chat_type, config):
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate("ignored_unauthorized", preview="Ignored non-private Telegram command update.")
    command = normalize_admin_command(_message_text(message))
    is_admin = _chat_matches_admin(chat_id, config.admin_chat_id)

    if not is_admin:
        return await _process_public_update(
            update_id,
            command,
            chat_id,
            config=config,
            command_service=command_service,
            transport=transport,
            state_path=state_path,
            audit_path=audit_path,
        )

    return await _process_admin_update(
        update_id,
        command,
        chat_id,
        config=config,
        command_service=command_service,
        transport=transport,
        state_path=state_path,
        audit_path=audit_path,
    )


async def _process_admin_wolf_publish_update(
    update_id: int,
    chat_id: str,
    *,
    config: TelegramAdminConfig,
    command_service: TelegramAdminCommandService,
    transport: TelegramAdminCommandTransport,
    state_path: Path,
    audit_path: Path,
) -> _ProcessedUpdate:
    if not bool(getattr(config, "wolf_briefing_enabled", False)):
        return await _send_admin_publish_notice(
            update_id,
            chat_id,
            "Wolf Briefing is disabled. Enable TELEGRAM_WOLF_BRIEFING_ENABLED=true before publishing.",
            response_type="wolf_briefing_disabled",
            delivery_status="skipped_disabled",
            config=config,
            transport=transport,
            state_path=state_path,
            audit_path=audit_path,
        )

    response = command_service.wolf_briefing_public_response(
        admin_config=config,
        command=WOLF_BRIEFING_PUBLISH_COMMAND,
    )
    skipped = _skip_status(config)
    if skipped is not None:
        _append_command_audit(audit_path, update_id, chat_id, response, skipped)
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate(skipped, preview=_preview(response.text))
    if config.dry_run:
        _append_command_audit(audit_path, update_id, chat_id, response, "dry_run")
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate("dry_run", preview=_preview(response.text))
    if not bool(getattr(config, "wolf_briefing_channel_publish_enabled", False)):
        return await _send_admin_publish_notice(
            update_id,
            chat_id,
            (
                "Wolf Briefing channel publishing is disabled. "
                "Enable TELEGRAM_WOLF_BRIEFING_CHANNEL_PUBLISH_ENABLED=true to publish manually."
            ),
            response_type="wolf_briefing_publish_disabled",
            delivery_status="skipped_disabled",
            config=config,
            transport=transport,
            state_path=state_path,
            audit_path=audit_path,
        )

    target_channel_id = _wolf_briefing_publish_channel_id(config)
    if target_channel_id == NA:
        return await _send_admin_publish_notice(
            update_id,
            chat_id,
            (
                "Wolf Briefing public channel is not configured. "
                "Set TELEGRAM_WOLF_BRIEFING_CHANNEL_ID or TELEGRAM_PUBLIC_CHANNEL_ID."
            ),
            response_type="wolf_briefing_publish_missing_target",
            delivery_status="skipped_missing_credentials",
            config=config,
            transport=transport,
            state_path=state_path,
            audit_path=audit_path,
            error_message="missing_wolf_briefing_publish_channel",
        )
    if _wolf_publish_target_is_unsafe(target_channel_id, chat_id, config):
        return await _send_admin_publish_notice(
            update_id,
            chat_id,
            (
                "Wolf Briefing public channel target is not safe to use. "
                "Set TELEGRAM_WOLF_BRIEFING_CHANNEL_ID or TELEGRAM_PUBLIC_CHANNEL_ID to a public signal channel."
            ),
            response_type="wolf_briefing_publish_unsafe_target",
            delivery_status="skipped_missing_credentials",
            config=config,
            transport=transport,
            state_path=state_path,
            audit_path=audit_path,
            error_message="unsafe_wolf_briefing_publish_channel",
        )

    try:
        raw_results = await transport.send_message(
            bot_token=config.bot_token or "",
            chat_id=target_channel_id,
            message=response.text,
            reply_markup=None,
            photo_path=None,
            photo_url=None,
        )
    except Exception as exc:
        error = _sanitize_error(exc, config, extra_secrets=(target_channel_id,))
        return await _send_admin_publish_notice(
            update_id,
            chat_id,
            f"Wolf Briefing publish failed: {error}",
            response_type="wolf_briefing_publish_failed",
            delivery_status="failed",
            config=config,
            transport=transport,
            state_path=state_path,
            audit_path=audit_path,
            error_message=error,
        )

    sanitized_results = tuple(
        _sanitize_result(item, config, extra_secrets=(target_channel_id,)) for item in raw_results
    )
    if _raw_results_sent(sanitized_results):
        _append_command_audit(audit_path, update_id, chat_id, response, "sent_public")
        _save_latest_processed_update_id(state_path, update_id)
        await _send_wolf_publish_confirmation(config=config, transport=transport)
        return _ProcessedUpdate("sent_public", sent=True, preview=_preview(response.text))

    error = _first_result_error(sanitized_results)
    return await _send_admin_publish_notice(
        update_id,
        chat_id,
        f"Wolf Briefing publish failed: {error}",
        response_type="wolf_briefing_publish_failed",
        delivery_status="failed",
        config=config,
        transport=transport,
        state_path=state_path,
        audit_path=audit_path,
        error_message=error,
    )


async def _send_admin_publish_notice(
    update_id: int,
    chat_id: str,
    message: str,
    *,
    response_type: str,
    delivery_status: str,
    config: TelegramAdminConfig,
    transport: TelegramAdminCommandTransport,
    state_path: Path,
    audit_path: Path,
    error_message: str = NA,
) -> _ProcessedUpdate:
    response = AdminCommandResponse(
        command=WOLF_BRIEFING_PUBLISH_COMMAND,
        response_type=response_type,
        text=message,
    )
    if config.dry_run:
        _append_command_audit(audit_path, update_id, chat_id, response, "dry_run", error_message=error_message)
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate("dry_run", preview=_preview(message), error_message=error_message)
    if not config.bot_token or not config.admin_chat_id:
        _append_command_audit(
            audit_path,
            update_id,
            chat_id,
            response,
            "skipped_missing_credentials",
            error_message=error_message,
        )
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate("skipped_missing_credentials", preview=_preview(message), error_message=error_message)
    try:
        raw_results = await transport.send_message(
            bot_token=config.bot_token or "",
            chat_id=config.admin_chat_id or "",
            message=message,
            reply_markup=None,
            photo_path=None,
            photo_url=None,
        )
    except Exception as exc:
        error = _sanitize_error(exc, config)
        _append_command_audit(audit_path, update_id, chat_id, response, "failed", error_message=error)
        return _ProcessedUpdate("failed", preview=_preview(message), error_message=error)

    sanitized_results = tuple(_sanitize_result(item, config) for item in raw_results)
    if _raw_results_sent(sanitized_results):
        _append_command_audit(audit_path, update_id, chat_id, response, delivery_status, error_message=error_message)
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate(delivery_status, sent=True, preview=_preview(message), error_message=error_message)

    error = _first_result_error(sanitized_results)
    _append_command_audit(audit_path, update_id, chat_id, response, "failed", error_message=error)
    return _ProcessedUpdate("failed", preview=_preview(message), error_message=error)


async def _send_wolf_publish_confirmation(
    *,
    config: TelegramAdminConfig,
    transport: TelegramAdminCommandTransport,
) -> None:
    if not config.bot_token or not config.admin_chat_id:
        return
    try:
        await transport.send_message(
            bot_token=config.bot_token or "",
            chat_id=config.admin_chat_id or "",
            message="Wolf Briefing published to public channel.",
            reply_markup=None,
            photo_path=None,
            photo_url=None,
        )
    except Exception:
        return


async def _process_admin_update(
    update_id: int,
    command: str,
    chat_id: str,
    *,
    config: TelegramAdminConfig,
    command_service: TelegramAdminCommandService,
    transport: TelegramAdminCommandTransport,
    state_path: Path,
    audit_path: Path,
) -> _ProcessedUpdate:
    response = command_service.response_for(command, admin_config=config)
    if _response_delivery_disabled(response):
        _append_command_audit(audit_path, update_id, chat_id, response, "skipped_disabled")
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate("skipped_disabled", preview=_preview(response.text))
    skipped = _skip_status(config)
    if skipped is not None:
        _append_command_audit(audit_path, update_id, chat_id, response, skipped)
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate(skipped, preview=_preview(response.text))
    if config.dry_run:
        _append_command_audit(audit_path, update_id, chat_id, response, "dry_run")
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate("dry_run", preview=_preview(response.text))

    await _cleanup_reply_keyboard_if_needed(
        config=config,
        chat_id=chat_id,
        response=response,
        transport=transport,
        state_path=state_path,
    )

    try:
        raw_results = await transport.send_message(
            bot_token=config.bot_token or "",
            chat_id=config.admin_chat_id or "",
            message=response.text,
            reply_markup=response.reply_markup,
            photo_path=response.photo_path,
            photo_url=response.photo_url,
        )
    except Exception as exc:
        error = _sanitize_error(exc, config)
        _append_command_audit(audit_path, update_id, chat_id, response, "failed", error_message=error)
        return _ProcessedUpdate("failed", preview=_preview(response.text), error_message=error)

    sanitized_results = tuple(_sanitize_result(item, config) for item in raw_results)
    sent = bool(sanitized_results) and all(item.get("status") == "sent" for item in sanitized_results)
    if sent:
        _append_command_audit(audit_path, update_id, chat_id, response, "sent_admin")
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate("sent_admin", sent=True, preview=_preview(response.text))

    error = _first_result_error(sanitized_results)
    _append_command_audit(audit_path, update_id, chat_id, response, "failed", error_message=error)
    return _ProcessedUpdate("failed", preview=_preview(response.text), error_message=error)


async def _process_public_update(
    update_id: int,
    command: str,
    chat_id: str,
    *,
    config: TelegramAdminConfig,
    command_service: TelegramAdminCommandService,
    transport: TelegramAdminCommandTransport,
    state_path: Path,
    audit_path: Path,
) -> _ProcessedUpdate:
    response = command_service.public_response_for(command, public_config=config)
    skipped = _public_skip_status(config, chat_id)
    if skipped is not None:
        _append_public_command_audit(audit_path, update_id, chat_id, response, skipped)
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate(skipped, preview=_preview(response.text))
    if config.dry_run:
        _append_public_command_audit(audit_path, update_id, chat_id, response, "dry_run")
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate("dry_run", preview=_preview(response.text))

    await _cleanup_reply_keyboard_if_needed(
        config=config,
        chat_id=chat_id,
        response=response,
        transport=transport,
        state_path=state_path,
    )

    try:
        raw_results = await _send_public_command_response(
            config=config,
            chat_id=chat_id,
            response=response,
            transport=transport,
        )
    except Exception as exc:
        error = _sanitize_error(exc, config, extra_secrets=(chat_id,))
        _append_public_command_audit(audit_path, update_id, chat_id, response, "failed", error_message=error)
        return _ProcessedUpdate("failed", preview=_preview(response.text), error_message=error)

    sanitized_results = tuple(_sanitize_result(item, config, extra_secrets=(chat_id,)) for item in raw_results)
    sent = bool(sanitized_results) and all(item.get("status") == "sent" for item in sanitized_results)
    if sent:
        _append_public_command_audit(audit_path, update_id, chat_id, response, "sent_public")
        _save_latest_processed_update_id(state_path, update_id)
        return _ProcessedUpdate("sent_public", sent=True, preview=_preview(response.text))

    error = _first_result_error(sanitized_results)
    _append_public_command_audit(audit_path, update_id, chat_id, response, "failed", error_message=error)
    return _ProcessedUpdate("failed", preview=_preview(response.text), error_message=error)


async def _send_public_command_response(
    *,
    config: TelegramAdminConfig,
    chat_id: str,
    response: AdminCommandResponse,
    transport: TelegramAdminCommandTransport,
) -> tuple[Mapping[str, Any], ...]:
    if response.photo_path is None and response.photo_url is None:
        return await transport.send_message(
            bot_token=config.bot_token or "",
            chat_id=chat_id,
            message=response.text,
            reply_markup=response.reply_markup,
            photo_path=None,
            photo_url=None,
        )

    if response.photo_path is not None:
        try:
            photo_results = await transport.send_message(
                bot_token=config.bot_token or "",
                chat_id=chat_id,
                message=response.text,
                reply_markup=response.reply_markup,
                photo_path=response.photo_path,
                photo_url=None,
            )
        except Exception:
            photo_results = ()
        if _raw_results_sent(photo_results):
            return photo_results
        return await _send_public_text_response(config=config, chat_id=chat_id, response=response, transport=transport)

    if response.photo_url is not None:
        try:
            photo_results = await transport.send_message(
                bot_token=config.bot_token or "",
                chat_id=chat_id,
                message=response.text,
                reply_markup=response.reply_markup,
                photo_path=None,
                photo_url=response.photo_url,
            )
        except Exception:
            photo_results = ()
        if _raw_results_sent(photo_results):
            return photo_results
    return await transport.send_message(
        bot_token=config.bot_token or "",
        chat_id=chat_id,
        message=response.text,
        reply_markup=response.reply_markup,
        photo_path=None,
        photo_url=None,
    )


async def _send_public_text_response(
    *,
    config: TelegramAdminConfig,
    chat_id: str,
    response: AdminCommandResponse,
    transport: TelegramAdminCommandTransport,
) -> tuple[Mapping[str, Any], ...]:
    return await transport.send_message(
        bot_token=config.bot_token or "",
        chat_id=chat_id,
        message=response.text,
        reply_markup=response.reply_markup,
        photo_path=None,
        photo_url=None,
    )


def _raw_results_sent(results: Sequence[Mapping[str, Any]]) -> bool:
    return bool(results) and all(result.get("status") == "sent" for result in results)


async def _cleanup_reply_keyboard_if_needed(
    *,
    config: TelegramAdminConfig,
    chat_id: str,
    response: AdminCommandResponse,
    transport: TelegramAdminCommandTransport,
    state_path: Path,
) -> None:
    if not response.cleanup_reply_keyboard:
        return
    if _display(chat_id) == NA or not config.bot_token:
        return
    if _reply_keyboard_cleanup_already_sent(state_path, chat_id):
        return
    try:
        results = await transport.send_message(
            bot_token=config.bot_token or "",
            chat_id=chat_id,
            message=REPLY_KEYBOARD_CLEANUP_MESSAGE,
            reply_markup=reply_keyboard_remove_markup(),
            photo_path=None,
            photo_url=None,
        )
    except Exception:
        return
    if _raw_results_sent(results):
        _mark_reply_keyboard_cleanup_sent(state_path, chat_id)


async def _answer_callback_query_safely(
    *,
    config: TelegramAdminConfig,
    callback_query_id: str,
    transport: TelegramAdminCommandTransport,
) -> None:
    if _display(callback_query_id) == NA or not config.bot_token or config.dry_run:
        return
    try:
        await transport.answer_callback_query(
            bot_token=config.bot_token or "",
            callback_query_id=callback_query_id,
        )
    except Exception:
        return


def _skip_status(config: TelegramAdminConfig) -> str | None:
    if not config.command_ui_enabled:
        return "skipped_disabled"
    if config.dry_run:
        return None
    if not config.has_admin_credentials:
        return "skipped_missing_credentials"
    return None


def _response_delivery_disabled(response: AdminCommandResponse) -> bool:
    return response.response_type == "wolf_briefing_disabled"


def _public_skip_status(config: TelegramAdminConfig, chat_id: str) -> str | None:
    if not config.command_ui_enabled or not config.public_command_ui_enabled:
        return "skipped_disabled"
    if _display(chat_id) == NA:
        return "ignored_unauthorized"
    if config.dry_run:
        return None
    if not config.bot_token:
        return "skipped_missing_credentials"
    return None


def _network_skip_status(config: TelegramAdminConfig) -> str | None:
    if not config.command_ui_enabled:
        return "skipped_disabled"
    if config.dry_run:
        return "dry_run"
    if not config.bot_token:
        return "skipped_missing_credentials"
    return None


def _updates_from_response(response: httpx.Response) -> tuple[Mapping[str, Any], ...]:
    if response.status_code == 429:
        raise RuntimeError("Telegram getUpdates was rate limited.")
    if response.status_code >= 400:
        raise RuntimeError(f"Telegram getUpdates returned HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Malformed Telegram getUpdates response.") from exc
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        description = payload.get("description") if isinstance(payload, Mapping) else None
        raise RuntimeError(f"Telegram getUpdates failed: {_display(description)}")
    results = payload.get("result")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        return ()
    return tuple(item for item in results if isinstance(item, Mapping))


async def _send_admin_photo_file_with_reply_markup(
    *,
    bot_token: str,
    chat_id: str,
    message: str,
    photo_path: Path,
    reply_markup: Mapping[str, Any] | None,
    http_client: httpx.AsyncClient | None,
    api_base_url: str,
    timeout: float,
) -> tuple[Mapping[str, Any], ...]:
    close_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=api_base_url, timeout=timeout)
    chunks = split_message(message, 1024)
    first_chunk = chunks[0] if chunks else message
    url = f"/bot{bot_token}/sendPhoto"
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "caption": first_chunk,
    }
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    try:
        try:
            handle = photo_path.open("rb")
        except OSError:
            return (
                _admin_send_failure(
                    part_number=1,
                    total_parts=len(chunks) or 1,
                    error="Telegram public welcome image file could not be opened.",
                ),
            )

        try:
            files = {"photo": (photo_path.name, handle, _photo_content_type(photo_path))}
            try:
                response = await client.post(url, data=data, files=files)
            except httpx.TimeoutException:
                return (
                    _admin_send_failure(
                        part_number=1,
                        total_parts=len(chunks) or 1,
                        error="Telegram public welcome image upload timed out.",
                    ),
                )
            except httpx.HTTPError as exc:
                return (
                    _admin_send_failure(
                        part_number=1,
                        total_parts=len(chunks) or 1,
                        error=f"Telegram public welcome image upload failed: {exc}",
                    ),
                )
        finally:
            handle.close()

        if response.status_code != 200:
            return (
                _admin_send_failure(
                    part_number=1,
                    total_parts=len(chunks) or 1,
                    error=_telegram_send_http_error(response),
                    http_status=response.status_code,
                    rate_limited=response.status_code == 429,
                ),
            )

        try:
            body = response.json()
        except ValueError:
            return (
                _admin_send_failure(
                    part_number=1,
                    total_parts=len(chunks) or 1,
                    error="Telegram public welcome image upload response could not be read.",
                    http_status=response.status_code,
                ),
            )

        if not isinstance(body, Mapping) or body.get("ok") is not True:
            return (
                _admin_send_failure(
                    part_number=1,
                    total_parts=len(chunks) or 1,
                    error=_telegram_malformed_body_error(body),
                    http_status=response.status_code,
                ),
            )

        results: list[Mapping[str, Any]] = [
            {
                "status": "sent",
                "part_number": 1,
                "total_parts": len(chunks) or 1,
                "http_status": response.status_code,
                "rate_limited": False,
                "error": None,
                **_telegram_success_metadata(body),
            }
        ]
    finally:
        if close_client:
            await client.aclose()

    if len(chunks) > 1:
        rest = "\n".join(chunks[1:])
        if rest:
            results.extend(
                await _send_admin_messages_with_reply_markup(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    message=rest,
                    reply_markup=reply_markup or {},
                    http_client=http_client,
                    api_base_url=api_base_url,
                    timeout=timeout,
                )
            )
    return tuple(results)


async def _send_admin_photo_with_reply_markup(
    *,
    bot_token: str,
    chat_id: str,
    message: str,
    photo_url: str,
    reply_markup: Mapping[str, Any] | None,
    http_client: httpx.AsyncClient | None,
    api_base_url: str,
    timeout: float,
) -> tuple[Mapping[str, Any], ...]:
    close_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=api_base_url, timeout=timeout)
    chunks = split_message(message, 1024)
    first_chunk = chunks[0] if chunks else message
    url = f"/bot{bot_token}/sendPhoto"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": first_chunk,
    }
    if reply_markup is not None:
        payload["reply_markup"] = dict(reply_markup)

    results: list[Mapping[str, Any]] = []
    try:
        try:
            response = await client.post(url, json=payload)
        except httpx.TimeoutException:
            return (
                _admin_send_failure(
                    part_number=1,
                    total_parts=len(chunks) or 1,
                    error="Telegram public welcome image request timed out.",
                ),
            )
        except httpx.HTTPError as exc:
            return (
                _admin_send_failure(
                    part_number=1,
                    total_parts=len(chunks) or 1,
                    error=f"Telegram public welcome image request failed: {exc}",
                ),
            )

        if response.status_code != 200:
            return (
                _admin_send_failure(
                    part_number=1,
                    total_parts=len(chunks) or 1,
                    error=_telegram_send_http_error(response),
                    http_status=response.status_code,
                    rate_limited=response.status_code == 429,
                ),
            )

        try:
            body = response.json()
        except ValueError:
            return (
                _admin_send_failure(
                    part_number=1,
                    total_parts=len(chunks) or 1,
                    error="Telegram public welcome image response could not be read.",
                    http_status=response.status_code,
                ),
            )

        if not isinstance(body, Mapping) or body.get("ok") is not True:
            return (
                _admin_send_failure(
                    part_number=1,
                    total_parts=len(chunks) or 1,
                    error=_telegram_malformed_body_error(body),
                    http_status=response.status_code,
                ),
            )

        results.append(
            {
                "status": "sent",
                "part_number": 1,
                "total_parts": len(chunks) or 1,
                "http_status": response.status_code,
                "rate_limited": False,
                "error": None,
                **_telegram_success_metadata(body),
            }
        )
    finally:
        if close_client:
            await client.aclose()

    if len(chunks) > 1:
        rest = "\n".join(chunks[1:])
        if rest:
            results.extend(
                await _send_admin_messages_with_reply_markup(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    message=rest,
                    reply_markup=reply_markup or {},
                    http_client=http_client,
                    api_base_url=api_base_url,
                    timeout=timeout,
                )
            )
    return tuple(results)


async def _send_admin_messages_with_reply_markup(
    *,
    bot_token: str,
    chat_id: str,
    message: str,
    reply_markup: Mapping[str, Any],
    http_client: httpx.AsyncClient | None,
    api_base_url: str,
    timeout: float,
    max_message_length: int = TELEGRAM_MAX_MESSAGE_LENGTH,
) -> tuple[Mapping[str, Any], ...]:
    chunks = split_message(message, max_message_length)
    close_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=api_base_url, timeout=timeout)
    url = f"/bot{bot_token}/sendMessage"
    results: list[Mapping[str, Any]] = []

    try:
        for part_number, chunk in enumerate(chunks, start=1):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
                "reply_markup": dict(reply_markup),
            }
            try:
                response = await client.post(url, json=payload)
            except httpx.TimeoutException:
                results.append(
                    _admin_send_failure(
                        part_number=part_number,
                        total_parts=len(chunks),
                        error="Telegram admin command request timed out.",
                    )
                )
                break
            except httpx.HTTPError as exc:
                results.append(
                    _admin_send_failure(
                        part_number=part_number,
                        total_parts=len(chunks),
                        error=f"Telegram admin command request failed: {exc}",
                    )
                )
                break

            if response.status_code != 200:
                results.append(
                    _admin_send_failure(
                        part_number=part_number,
                        total_parts=len(chunks),
                        error=_telegram_send_http_error(response),
                        http_status=response.status_code,
                        rate_limited=response.status_code == 429,
                    )
                )
                break

            try:
                body = response.json()
            except ValueError:
                results.append(
                    _admin_send_failure(
                        part_number=part_number,
                        total_parts=len(chunks),
                        error="Malformed Telegram admin command response.",
                        http_status=response.status_code,
                    )
                )
                break

            if not isinstance(body, Mapping) or body.get("ok") is not True:
                results.append(
                    _admin_send_failure(
                        part_number=part_number,
                        total_parts=len(chunks),
                        error=_telegram_malformed_body_error(body),
                        http_status=response.status_code,
                    )
                )
                break

            results.append(
                {
                    "status": "sent",
                    "part_number": part_number,
                    "total_parts": len(chunks),
                    "http_status": response.status_code,
                    "rate_limited": False,
                    "error": None,
                    **_telegram_success_metadata(body),
                }
            )
    finally:
        if close_client:
            await client.aclose()

    return tuple(results)


async def _answer_callback_query(
    *,
    bot_token: str,
    callback_query_id: str,
    text: str | None,
    http_client: httpx.AsyncClient | None,
    api_base_url: str,
    timeout: float,
) -> Mapping[str, Any]:
    close_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=api_base_url, timeout=timeout)
    url = f"/bot{bot_token}/answerCallbackQuery"
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]

    try:
        try:
            response = await client.post(url, json=payload)
        except httpx.TimeoutException:
            return {"status": "failed", "error": "Telegram callback acknowledgement timed out."}
        except httpx.HTTPError as exc:
            return {"status": "failed", "error": f"Telegram callback acknowledgement failed: {exc}"}

        if response.status_code != 200:
            return {
                "status": "failed",
                "http_status": response.status_code,
                "rate_limited": response.status_code == 429,
                "error": _telegram_send_http_error(response),
            }

        try:
            body = response.json()
        except ValueError:
            return {
                "status": "failed",
                "http_status": response.status_code,
                "error": "Malformed Telegram callback acknowledgement response.",
            }

        if not isinstance(body, Mapping) or body.get("ok") is not True:
            return {
                "status": "failed",
                "http_status": response.status_code,
                "error": _telegram_malformed_body_error(body),
            }

        return {"status": "sent", "http_status": response.status_code, "rate_limited": False, "error": None}
    finally:
        if close_client:
            await client.aclose()


def _admin_send_failure(
    *,
    part_number: int,
    total_parts: int,
    error: str,
    http_status: int | None = None,
    rate_limited: bool = False,
) -> Mapping[str, Any]:
    return {
        "status": "failed",
        "part_number": part_number,
        "total_parts": total_parts,
        "http_status": http_status,
        "rate_limited": rate_limited,
        "error": error,
    }


def _photo_content_type(path: Path) -> str:
    content_type, _encoding = mimetypes.guess_type(str(path))
    return content_type or "application/octet-stream"


def _telegram_send_http_error(response: httpx.Response) -> str:
    if response.status_code == 429:
        return "Telegram rate limited the admin command request."
    return f"Telegram returned HTTP {response.status_code}."


def _telegram_malformed_body_error(body: Any) -> str:
    if isinstance(body, Mapping):
        description = body.get("description")
        if isinstance(description, str) and description.strip():
            return f"Telegram response did not confirm success: {description.strip()}"
    return "Malformed Telegram response."


def _telegram_success_metadata(body: Mapping[str, Any]) -> dict[str, Any]:
    result = body.get("result")
    if not isinstance(result, Mapping):
        return {}

    metadata: dict[str, Any] = {}
    message_id = result.get("message_id")
    if message_id is not None:
        metadata["message_id"] = message_id

    chat = result.get("chat")
    if isinstance(chat, Mapping):
        chat_id = chat.get("id")
        if chat_id is not None:
            metadata["chat_id"] = chat_id

    sent_at = _telegram_sent_at(result.get("date"))
    if sent_at is not None:
        metadata["sent_at"] = sent_at

    return metadata


def _telegram_sent_at(value: Any) -> str | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def _limited_new_updates(
    updates: Sequence[Mapping[str, Any]],
    latest_processed_update_id: int | None,
    limit: int,
) -> tuple[Mapping[str, Any], ...]:
    output: list[Mapping[str, Any]] = []
    for update in sorted(updates, key=lambda item: _update_id(item) if _update_id(item) is not None else -1):
        update_id = _update_id(update)
        if update_id is None:
            continue
        if latest_processed_update_id is not None and update_id <= latest_processed_update_id:
            continue
        output.append(update)
        if len(output) >= limit:
            break
    return tuple(output)


def _append_command_audit(
    audit_path: Path,
    update_id: int,
    chat_id: str,
    response: AdminCommandResponse,
    delivery_status: str,
    *,
    error_message: str = NA,
) -> None:
    _append_audit_record(
        audit_path,
        update_id=update_id,
        command=response.command,
        chat_id=chat_id,
        is_admin=True,
        response_type=response.response_type,
        delivery_status=delivery_status,
        response_preview=_preview(response.text),
        run_id=response.run_id,
        error_message=error_message,
    )


def _append_public_command_audit(
    audit_path: Path,
    update_id: int,
    chat_id: str,
    response: AdminCommandResponse,
    delivery_status: str,
    *,
    error_message: str = NA,
) -> None:
    _append_audit_record(
        audit_path,
        update_id=update_id,
        command=response.command,
        chat_id=chat_id,
        is_admin=False,
        response_type=response.response_type,
        delivery_status=delivery_status,
        response_preview=_preview(response.text),
        run_id=response.run_id,
        error_message=error_message,
    )


def _append_audit_record(
    audit_path: Path,
    *,
    update_id: int,
    command: str,
    chat_id: str,
    is_admin: bool,
    response_type: str,
    delivery_status: str,
    response_preview: str,
    run_id: str = NA,
    error_message: str = NA,
) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": _now_utc_iso(),
        "update_id": update_id,
        "command": _display(command),
        "chat_id_hash": _chat_id_hash(chat_id),
        "is_admin": is_admin,
        "response_type": response_type,
        "delivery_status": delivery_status,
        "error_message": _display(error_message),
        "run_id": _display(run_id),
        "response_preview": _display(response_preview),
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
        handle.write("\n")


def _load_latest_processed_update_id(path: Path) -> int | None:
    payload = _load_command_state(path)
    value = payload.get("latest_processed_update_id")
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _save_latest_processed_update_id(path: Path, update_id: int) -> None:
    current = _load_latest_processed_update_id(path)
    if current is not None and current >= update_id:
        return
    payload = _load_command_state(path)
    payload["latest_processed_update_id"] = update_id
    payload["updated_at"] = _now_utc_iso()
    _save_command_state(path, payload)


def _reply_keyboard_cleanup_already_sent(path: Path, chat_id: str) -> bool:
    chat_hash = _chat_id_hash(chat_id)
    if chat_hash == NA:
        return True
    values = _load_command_state(path).get(REPLY_KEYBOARD_CLEANUP_STATE_KEY)
    return isinstance(values, list) and chat_hash in values


def _mark_reply_keyboard_cleanup_sent(path: Path, chat_id: str) -> None:
    chat_hash = _chat_id_hash(chat_id)
    if chat_hash == NA:
        return
    payload = _load_command_state(path)
    values = payload.get(REPLY_KEYBOARD_CLEANUP_STATE_KEY)
    hashes = [str(value) for value in values] if isinstance(values, list) else []
    if chat_hash not in hashes:
        hashes.append(chat_hash)
    payload[REPLY_KEYBOARD_CLEANUP_STATE_KEY] = sorted(hashes)
    payload["updated_at"] = _now_utc_iso()
    _save_command_state(path, payload)


def _load_command_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    return dict(payload)


def _save_command_state(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")


def _message_payload(update: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("message", "edited_message", "channel_post"):
        value = update.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _callback_query_payload(update: Mapping[str, Any]) -> Mapping[str, Any]:
    value = update.get("callback_query")
    return value if isinstance(value, Mapping) else {}


def _callback_query_id(callback_query: Mapping[str, Any]) -> str:
    return _display(callback_query.get("id"))


def _callback_data(callback_query: Mapping[str, Any]) -> str:
    return _display(callback_query.get("data"))


def _callback_chat_id(callback_query: Mapping[str, Any]) -> str:
    message = callback_query.get("message")
    if isinstance(message, Mapping):
        chat_id = _chat_id(message)
        if chat_id != NA:
            return chat_id
    sender = callback_query.get("from")
    if isinstance(sender, Mapping):
        return _display(sender.get("id"))
    return NA


def _callback_chat_type(callback_query: Mapping[str, Any]) -> str:
    message = callback_query.get("message")
    if isinstance(message, Mapping):
        return _chat_type(message)
    return NA


def _update_id(update: Mapping[str, Any]) -> int | None:
    try:
        return int(str(update.get("update_id")))
    except (TypeError, ValueError):
        return None


def _chat_id(message: Mapping[str, Any]) -> str:
    chat = message.get("chat")
    if isinstance(chat, Mapping):
        return _display(chat.get("id"))
    return NA


def _chat_type(message: Mapping[str, Any]) -> str:
    chat = message.get("chat")
    if isinstance(chat, Mapping):
        return _display(chat.get("type")).lower()
    return NA


def _message_text(message: Mapping[str, Any]) -> str:
    return _display(message.get("text"))


def _command_chat_is_private(chat_id: str, chat_type: str, config: TelegramAdminConfig) -> bool:
    if _display(chat_id) == NA:
        return False
    if _display(chat_id) in {
        _display(config.public_channel_id),
        _display(config.vip_channel_id),
        _display(getattr(config, "wolf_briefing_publish_channel_id", None)),
    }:
        return False
    normalized_type = _display(chat_type).lower()
    if normalized_type in {"group", "supergroup", "channel"}:
        return False
    return normalized_type in {NA.lower(), "private"}


def _chat_matches_admin(chat_id: str, admin_chat_id: str | None) -> bool:
    if not admin_chat_id:
        return False
    return _display(chat_id) == str(admin_chat_id).strip()


def _wolf_briefing_publish_channel_id(config: TelegramAdminConfig) -> str:
    return _display(getattr(config, "wolf_briefing_publish_channel_id", None))


def _wolf_publish_target_is_unsafe(target_channel_id: str, chat_id: str, config: TelegramAdminConfig) -> bool:
    target = _display(target_channel_id)
    return target == NA or target in {
        _display(chat_id),
        _display(config.admin_chat_id),
        _display(config.public_chat_id),
    }


def _chat_id_hash(chat_id: str) -> str:
    text = _display(chat_id)
    if text == NA:
        return NA
    return sha256(text.encode("utf-8")).hexdigest()[:16]


def _sanitize_result(
    result: Mapping[str, Any],
    config: TelegramAdminConfig,
    *,
    extra_secrets: Sequence[str] = (),
) -> dict[str, Any]:
    sanitized = {key: result[key] for key in ("status", "http_status", "rate_limited", "error") if key in result}
    if "error" in sanitized:
        sanitized["error"] = _sanitize_error(sanitized.get("error"), config, extra_secrets=extra_secrets)
    return sanitized


def _sanitize_error(
    value: Any,
    config: TelegramAdminConfig,
    *,
    extra_secrets: Sequence[str] = (),
) -> str:
    text = str(value or "").strip()
    if not text:
        return NA
    for secret in (
        config.bot_token,
        config.admin_chat_id,
        config.public_chat_id,
        config.public_channel_id,
        config.wolf_briefing_channel_id,
        config.signal_channel_invite_link,
        config.vip_channel_id,
        *extra_secrets,
    ):
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    return text


def _first_result_error(results: Sequence[Mapping[str, Any]]) -> str:
    for result in results:
        error = _display(result.get("error"))
        if error != NA:
            return error
    return "telegram_admin_command_delivery_failed"


def _preview_tuple(show_preview: bool, text: str) -> tuple[str, ...]:
    return (_preview(text),) if show_preview else ()


def _preview(text: str, max_length: int = 700) -> str:
    compact = " ".join(str(text).split())
    if not compact:
        return NA
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 3].rstrip()}..."


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    return str(value)


def _now_utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ADMIN_COMMAND_DELIVERY_STATUSES",
    "DEFAULT_ADMIN_COMMAND_AUDIT_PATH",
    "DEFAULT_ADMIN_COMMAND_STATE_PATH",
    "DEFAULT_ADMIN_COMMANDS_DIR",
    "DEFAULT_COMMAND_LIMIT",
    "AdminCommandProcessingResult",
    "HttpxTelegramAdminCommandTransport",
    "TelegramAdminCommandTransport",
    "process_telegram_admin_commands",
]
