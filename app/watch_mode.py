from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.alerts.integrity_manifest import AlertIntegrityManifest, build_alert_integrity_manifest
from app.alerts.telegram_sender import TelegramSender
from app.analytics.portfolio_selection import PortfolioSelectionResult, selected_symbols
from app.analytics.setup_quality import SetupQualityState
from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display
from app.formatters.telegram_signal_formatter import TelegramSignalMessage, format_watchlist_upgraded_message
from app.lifecycle.eligibility import has_valid_trade_map, public_watchlist_eligible
from app.pipeline.scanner_runner import ScannerRunResult, ScannerSymbolResult

DEFAULT_WATCH_STATE_PATH = Path("scan_runs/watch_state.json")
DEFAULT_LATEST_RUN_PATH = Path("scan_runs/latest_scan.json")

VALID_QUALITY_STATES = {
    SetupQualityState.HIGH_QUALITY_TRADE.value,
    SetupQualityState.VALID_BUT_LOWER_QUALITY.value,
}
WATCH_READINESS_LABELS = {"HOT WATCH", "WATCH"}
WATCH_SOURCE_STATUSES = {"near_miss"}
PREVIOUS_ALERTABLE_STATUSES = {
    "near_miss",
    "no_setup",
    "rejected",
    "watch",
    "hot_watch",
    "watchlist_near_miss",
    "WATCHLIST_NEAR_MISS",
    "REJECTED_NO_EDGE",
}
PREVIOUS_ALERTABLE_READINESS = {"HOT WATCH", "WATCH", "REJECTED"}


class WatchModeError(ValueError):
    """Raised when watch mode cannot load or persist its local state."""


class WatchHistoryEntry(BaseModel):
    seen_at: str
    status: str
    failed_gate: str = NA
    readiness_score: int = 0
    readiness_label: str = NA
    quality_state: str = NA
    alert_triggered: bool = False

    model_config = ConfigDict(frozen=True)


class WatchSymbolState(BaseModel):
    symbol: str
    last_status: str = NA
    last_failed_gate: str = NA
    readiness_score: int = 0
    readiness_label: str = NA
    last_seen_at: str = NA
    alert_sent: bool = False
    invalidated: bool = False
    activation_count: int = 0
    history: tuple[WatchHistoryEntry, ...] = ()

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @model_validator(mode="after")
    def _derive_invalidated(self) -> WatchSymbolState:
        if self.invalidated:
            return self
        if _state_status_is_invalidated(self.last_status, self.readiness_label):
            object.__setattr__(self, "invalidated", True)
        return self


class WatchState(BaseModel):
    updated_at: str = NA
    symbols: dict[str, WatchSymbolState] = Field(default_factory=dict)
    deprecated: bool = True
    source_of_truth: str = "db_lifecycle_state"
    deprecation_note: str = (
        "watch_state.json is retained for compatibility only; DB-backed lifecycle state and scan run manifests are "
        "the source of truth for audits."
    )

    model_config = ConfigDict(frozen=True)


class WatchActivation(BaseModel):
    symbol: str
    mode: str
    message: str
    delivery_status: Literal["dry_run", "sent", "failed"]
    delivery_detail: str
    integrity_manifest: AlertIntegrityManifest | None = None

    model_config = ConfigDict(frozen=True)


class WatchIterationSummary(BaseModel):
    iteration: int
    scanned_at: str
    symbols_watched: int
    valid_activations: int
    still_watching: int
    rejected_no_edge: int
    data_issues: int
    iteration_id: str = NA
    status: Literal["SUCCESS", "PARTIAL", "FAILED", "CANCELLED", "FATAL"] = "SUCCESS"
    scheduled_start: str = NA
    actual_start: str = NA
    finished_at: str = NA
    duration_seconds: float = 0.0
    sleep_seconds: float = 0.0
    cadence_lag_seconds: float = 0.0
    overrun_seconds: float = 0.0
    missed_interval_count: int = 0
    consecutive_failure_count: int = 0
    selected_backoff_seconds: float = 0.0
    next_scheduled_attempt: str = NA
    queue_total: int = 0
    outcome_counts: dict[str, int] = Field(default_factory=dict)
    symbol_outcomes: dict[str, dict[str, str]] = Field(default_factory=dict)
    phase_statuses: dict[str, str] = Field(default_factory=dict)
    errors: tuple[str, ...] = ()
    active_lifecycle_count: int = 0
    telegram_outbox_status: dict[str, int] = Field(default_factory=dict)
    database_storage_status: str = "NOT_REQUESTED"
    next_scan_seconds: float | None = None
    activated_symbols: tuple[str, ...] = ()
    alerts: tuple[WatchActivation, ...] = ()

    model_config = ConfigDict(frozen=True)


class WatchAlertDelivery(BaseModel):
    status: Literal["dry_run", "sent", "failed"]
    detail: str
    telegram_results: tuple[dict[str, Any], ...] = ()

    model_config = ConfigDict(frozen=True)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_watch_state(path: Path = DEFAULT_WATCH_STATE_PATH) -> WatchState:
    if not path.exists():
        return WatchState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WatchModeError(f"watch state must be valid JSON: {path}") from exc
    try:
        return WatchState.model_validate(payload)
    except Exception as exc:
        raise WatchModeError(f"watch state has an invalid shape: {path}") from exc


def save_watch_state(
    path: Path,
    state: WatchState,
    *,
    expected_updated_at: str | None = None,
) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            current = load_watch_state(path)
            if (
                expected_updated_at not in (None, NA)
                and current.updated_at not in (NA, expected_updated_at)
                and current.updated_at > expected_updated_at
            ):
                raise WatchModeError(f"refusing to overwrite newer watch state: {path}")
            if current.updated_at != NA and state.updated_at != NA and current.updated_at > state.updated_at:
                raise WatchModeError(f"refusing to overwrite newer watch state: {path}")
        temporary_path.write_text(
            json.dumps(state.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except OSError as exc:
        try:
            if temporary_path.exists():
                temporary_path.unlink()
        except OSError as cleanup_exc:
            raise WatchModeError(
                f"could not write watch state and could not remove temporary file: {path}"
            ) from cleanup_exc
        raise WatchModeError(f"could not write watch state: {path}") from exc


def load_run_payload(path: Path = DEFAULT_LATEST_RUN_PATH) -> dict[str, Any]:
    if not path.exists():
        raise WatchModeError(f"latest saved run file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WatchModeError(f"latest saved run file must be valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise WatchModeError(f"latest saved run file must contain a JSON object: {path}")
    if not isinstance(payload.get("results"), list):
        raise WatchModeError(f"latest saved run file must contain a results list: {path}")
    return payload


def symbols_from_run_payload(
    payload: Mapping[str, Any],
    *,
    near_miss_only: bool = False,
) -> tuple[str, ...]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
        raise WatchModeError("saved run payload must contain a results list.")

    symbols: list[str] = []
    for raw_result in raw_results:
        if not isinstance(raw_result, Mapping):
            continue
        symbol = _symbol_from_payload(raw_result)
        if symbol == NA:
            continue
        if near_miss_only and not payload_is_watch_candidate(raw_result):
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)


def load_symbols_from_run(
    path: Path = DEFAULT_LATEST_RUN_PATH,
    *,
    near_miss_only: bool = False,
) -> tuple[str, ...]:
    return symbols_from_run_payload(load_run_payload(path), near_miss_only=near_miss_only)


def payload_is_watch_candidate(raw_result: Mapping[str, Any]) -> bool:
    snapshot = _snapshot_from_payload(raw_result)
    return (
        snapshot["status"] in WATCH_SOURCE_STATUSES
        or snapshot["readiness_label"] in WATCH_READINESS_LABELS
        or snapshot["quality_state"] == SetupQualityState.WATCHLIST_NEAR_MISS.value
    )


def seed_watch_state_from_run_payload(
    state: WatchState,
    payload: Mapping[str, Any],
    symbols: Sequence[str],
    *,
    seen_at: str | None = None,
) -> WatchState:
    target_symbols = {symbol.upper() for symbol in symbols}
    if not target_symbols:
        return state

    updated_symbols = dict(state.symbols)
    timestamp = seen_at or now_utc_iso()
    raw_results = payload.get("results", ())
    if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
        return state

    for raw_result in raw_results:
        if not isinstance(raw_result, Mapping):
            continue
        symbol = _symbol_from_payload(raw_result)
        if symbol not in target_symbols or symbol in updated_symbols:
            continue
        snapshot = _snapshot_from_payload(raw_result)
        updated_symbols[symbol] = WatchSymbolState(
            symbol=symbol,
            last_status=snapshot["status"],
            last_failed_gate=snapshot["failed_gate"],
            readiness_score=snapshot["readiness_score"],
            readiness_label=snapshot["readiness_label"],
            last_seen_at=timestamp,
            history=(
                WatchHistoryEntry(
                    seen_at=timestamp,
                    status=snapshot["status"],
                    failed_gate=snapshot["failed_gate"],
                    readiness_score=snapshot["readiness_score"],
                    readiness_label=snapshot["readiness_label"],
                    quality_state=snapshot["quality_state"],
                    alert_triggered=False,
                ),
            ),
        )

    return WatchState(updated_at=timestamp, symbols=updated_symbols)


def state_watch_symbols(state: WatchState, symbols: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for symbol in symbols:
        normalized = symbol.upper()
        symbol_state = state.symbols.get(normalized)
        if symbol_state is None:
            continue
        if symbol_state.invalidated:
            continue
        if _state_is_watch_candidate(symbol_state) and normalized not in output:
            output.append(normalized)
    return tuple(output)


def should_trigger_activation_alert(
    symbol_result: ScannerSymbolResult,
    previous_state: WatchSymbolState | None,
    *,
    portfolio_selection: PortfolioSelectionResult | None = None,
) -> bool:
    if previous_state is None:
        return False
    if previous_state.invalidated:
        return False
    if previous_state.alert_sent:
        return False
    if not _previous_state_allows_activation(previous_state):
        return False
    if not current_result_is_valid_activation(symbol_result, portfolio_selection=portfolio_selection):
        return False
    return True


def current_result_is_valid_activation(
    symbol_result: ScannerSymbolResult,
    *,
    portfolio_selection: PortfolioSelectionResult | None = None,
) -> bool:
    if symbol_result.trade_idea is None:
        return False
    quality = symbol_result.setup_quality
    quality_state = _quality_state_text(quality)
    if quality_state not in VALID_QUALITY_STATES:
        return False
    quality_gate = getattr(symbol_result.trade_idea, "quality_gate_result", None)
    if quality_gate is not None and getattr(quality_gate, "passed", True) is not True:
        return False
    display = build_symbol_display(symbol_result)
    if display.display_status != "valid_setup":
        return False
    if portfolio_selection is not None and symbol_result.symbol not in selected_symbols(portfolio_selection):
        return False
    return True


def update_watch_state_for_result(
    state: WatchState,
    symbol_result: ScannerSymbolResult,
    *,
    alert_triggered: bool,
    seen_at: str | None = None,
) -> WatchState:
    timestamp = seen_at or now_utc_iso()
    display = build_symbol_display(symbol_result)
    quality_state = _quality_state_text(symbol_result.setup_quality)
    previous = state.symbols.get(symbol_result.symbol)
    previous_history = previous.history if previous is not None else ()
    previous_alert_sent = previous.alert_sent if previous is not None else False
    previous_activation_count = previous.activation_count if previous is not None else 0
    previous_invalidated = previous.invalidated if previous is not None else False
    invalidated = previous_invalidated or _current_result_invalidated(display.display_status, quality_state)

    history_entry = WatchHistoryEntry(
        seen_at=timestamp,
        status=display.display_status,
        failed_gate=display.failed_gate,
        readiness_score=display.readiness_score,
        readiness_label=display.readiness_label,
        quality_state=quality_state,
        alert_triggered=alert_triggered,
    )
    updated_symbol = WatchSymbolState(
        symbol=symbol_result.symbol,
        last_status=display.display_status,
        last_failed_gate=display.failed_gate,
        readiness_score=display.readiness_score,
        readiness_label=display.readiness_label,
        last_seen_at=timestamp,
        alert_sent=previous_alert_sent or alert_triggered,
        invalidated=invalidated,
        activation_count=previous_activation_count + (1 if alert_triggered else 0),
        history=(*previous_history, history_entry),
    )
    updated_symbols = dict(state.symbols)
    updated_symbols[symbol_result.symbol] = updated_symbol
    return WatchState(updated_at=timestamp, symbols=updated_symbols)


def format_watch_activation_alert(symbol_result: ScannerSymbolResult) -> str:
    trade_idea = symbol_result.trade_idea
    if trade_idea is None:
        raise WatchModeError("cannot format activation alert without a trade idea")

    message = TelegramSignalMessage(
        symbol=symbol_result.symbol,
        direction=getattr(trade_idea, "direction", NA),
        mode=_activation_mode(symbol_result),
        quality=_quality_grade_text(symbol_result.setup_quality),
        entry_low=_level_field(getattr(trade_idea, "entry_zone", None), "low"),
        entry_high=_level_field(getattr(trade_idea, "entry_zone", None), "high"),
        stop_loss=_level_field(getattr(trade_idea, "stop_loss", None), "price"),
        tp1=_take_profit_price(trade_idea, 1),
        tp2=_take_profit_price(trade_idea, 2),
        tp3=_take_profit_price(trade_idea, 3),
        planned_rr=getattr(trade_idea, "best_rr", NA),
        structure_reason=_short_reason(getattr(trade_idea, "reason_for_trade", NA)),
        invalidation_reason=getattr(trade_idea, "invalidation", NA),
        upgraded_from_watchlist=True,
    )
    return format_watchlist_upgraded_message(message)


def build_watch_activation_alert_manifest(
    symbol_result: ScannerSymbolResult,
    *,
    message: str,
    delivery_status: Literal["dry_run", "sent", "failed"],
    live: bool,
) -> AlertIntegrityManifest:
    trade_idea = symbol_result.trade_idea
    if trade_idea is None:
        raise WatchModeError("cannot build activation alert manifest without a trade idea")
    return build_alert_integrity_manifest(
        trade_idea=trade_idea,
        formatted_message=message,
        message_parts=(message,),
        channel="telegram",
        status=delivery_status,
        dry_run=not live,
        deduplication_key=f"watch-activation-{symbol_result.symbol}-{_activation_mode(symbol_result)}",
    )


async def deliver_watch_activation_alert(
    message: str,
    *,
    live: bool,
    dry_run: bool = True,
    telegram_bot_token: str | None = None,
    telegram_chat_id: str | None = None,
) -> WatchAlertDelivery:
    if not live:
        return WatchAlertDelivery(status="dry_run", detail="Dry run: no Telegram alert was sent.")
    if not dry_run and (not telegram_bot_token or not telegram_chat_id):
        raise WatchModeError(
            "Live Telegram watch alerts require TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env."
        )

    send_result = await TelegramSender(
        bot_token=telegram_bot_token,
        chat_id=telegram_chat_id,
        signals_enabled=True,
        dry_run=dry_run,
    ).send_text(message)
    if send_result.error_message == "telegram_dry_run_enabled":
        return WatchAlertDelivery(status="dry_run", detail="Dry run: no Telegram alert was sent.")
    return WatchAlertDelivery(
        status="sent" if send_result.sent else "failed",
        detail="Telegram alert sent." if send_result.sent else "Telegram alert failed.",
        telegram_results=send_result.telegram_results,
    )


def build_watch_iteration_summary(
    *,
    iteration: int,
    result: ScannerRunResult,
    activations: Sequence[WatchActivation],
    next_scan_seconds: float | None,
    scanned_at: str | None = None,
) -> WatchIterationSummary:
    data_issues = 0
    still_watching = 0
    rejected_no_edge = 0
    activated = {activation.symbol for activation in activations}

    for symbol_result in result.results:
        display = build_symbol_display(symbol_result)
        quality_state = _quality_state_text(symbol_result.setup_quality)
        if display.display_status in {"data_issue", "scan_error"} or display.readiness_label == "DATA ISSUE":
            data_issues += 1
            continue
        if symbol_result.symbol in activated:
            continue
        if public_watchlist_eligible(symbol_result) and has_valid_trade_map(symbol_result):
            still_watching += 1
            continue
        if display.display_status == "no_setup" or quality_state == SetupQualityState.REJECTED_NO_EDGE.value:
            rejected_no_edge += 1

    return WatchIterationSummary(
        iteration=iteration,
        scanned_at=scanned_at or now_utc_iso(),
        symbols_watched=len(result.config.symbols),
        valid_activations=len(activations),
        still_watching=still_watching,
        rejected_no_edge=rejected_no_edge,
        data_issues=data_issues,
        next_scan_seconds=next_scan_seconds,
        activated_symbols=tuple(activation.symbol for activation in activations),
        alerts=tuple(activations),
    )


def format_watch_iteration_summary(summary: WatchIterationSummary) -> str:
    """Compatibility wrapper for callers that still request verbose diagnostics."""
    from app.formatters.scanner_console import format_watch_iteration_console

    return format_watch_iteration_console(summary, mode="verbose")


def append_watch_output(path: Path, summary: WatchIterationSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False))
        handle.write("\n")


def _snapshot_from_payload(raw_result: Mapping[str, Any]) -> dict[str, Any]:
    try:
        symbol_result = ScannerSymbolResult.model_validate(raw_result)
    except Exception:
        symbol_result = None

    if symbol_result is not None:
        display = build_symbol_display(symbol_result)
        quality_state = _quality_state_text(symbol_result.setup_quality)
        return {
            "status": _display(raw_result.get("display_status", display.display_status)),
            "failed_gate": display.failed_gate,
            "readiness_score": _int_text(raw_result.get("readiness_score", display.readiness_score)),
            "readiness_label": _display(raw_result.get("readiness_label", display.readiness_label)),
            "quality_state": quality_state,
        }

    return {
        "status": _display(raw_result.get("display_status", raw_result.get("last_status", raw_result.get("status")))),
        "failed_gate": _display(raw_result.get("failed_gate", raw_result.get("last_failed_gate"))),
        "readiness_score": _int_text(raw_result.get("readiness_score", 0)),
        "readiness_label": _display(raw_result.get("readiness_label")),
        "quality_state": _quality_state_from_payload(raw_result),
    }


def _symbol_from_payload(raw_result: Mapping[str, Any]) -> str:
    symbol = _display(raw_result.get("symbol"))
    return symbol.upper() if symbol != NA else NA


def _quality_state_from_payload(raw_result: Mapping[str, Any]) -> str:
    quality = raw_result.get("setup_quality")
    if isinstance(quality, Mapping):
        return _display(quality.get("quality_state"))
    return NA


def _state_is_watch_candidate(symbol_state: WatchSymbolState) -> bool:
    if symbol_state.invalidated:
        return False
    return (
        symbol_state.last_status in WATCH_SOURCE_STATUSES
        or symbol_state.readiness_label in WATCH_READINESS_LABELS
        or symbol_state.last_status == SetupQualityState.WATCHLIST_NEAR_MISS.value
    )


def _state_status_is_invalidated(status: str, readiness_label: str) -> bool:
    normalized_status = status.strip().lower().replace(" ", "_").replace("-", "_")
    return normalized_status in {"no_setup", "rejected", "rejected_no_edge"} or readiness_label == "REJECTED"


def _current_result_invalidated(display_status: str, quality_state: str) -> bool:
    return display_status == "no_setup" or quality_state == SetupQualityState.REJECTED_NO_EDGE.value


def _previous_state_allows_activation(symbol_state: WatchSymbolState) -> bool:
    if symbol_state.last_status == "valid_setup" or symbol_state.readiness_label == "VALID SETUP":
        return False
    normalized_status = symbol_state.last_status.strip().lower().replace(" ", "_").replace("-", "_")
    return (
        symbol_state.last_status in PREVIOUS_ALERTABLE_STATUSES
        or normalized_status in PREVIOUS_ALERTABLE_STATUSES
        or symbol_state.readiness_label in PREVIOUS_ALERTABLE_READINESS
    )


def _quality_state_text(quality: Any) -> str:
    state = getattr(quality, "quality_state", NA)
    return getattr(state, "value", state)


def _quality_grade_text(quality: Any) -> str:
    grade = getattr(quality, "quality_grade", NA)
    return _display(getattr(grade, "value", grade))


def _quality_score_text(quality: Any) -> str:
    return _display(getattr(quality, "quality_score", NA))


def _activation_mode(symbol_result: ScannerSymbolResult) -> str:
    if symbol_result.valid_strategy_modes:
        return symbol_result.valid_strategy_modes[0].upper()
    trade_idea = symbol_result.trade_idea
    setup_type = _display(getattr(trade_idea, "setup_type", NA))
    for mode in ("challenge", "swing", "scalp"):
        if setup_type.endswith(f"_{mode}") or mode in setup_type:
            return mode.upper()
    return NA


def _level_text(level: Any) -> str:
    price = _display(getattr(level, "price", NA))
    low = _display(getattr(level, "low", NA))
    high = _display(getattr(level, "high", NA))
    if price != NA:
        return price
    if low != NA and high != NA:
        return low if low == high else f"{low} - {high}"
    if low != NA:
        return low
    if high != NA:
        return high
    return NA


def _take_profit_text(trade_idea: Any, target_number: int) -> str:
    targets = getattr(trade_idea, "take_profits", ())
    if not isinstance(targets, Sequence):
        return NA
    index = target_number - 1
    if index >= len(targets):
        return NA
    return _display(getattr(targets[index], "price", NA))


def _take_profit_price(trade_idea: Any, target_number: int) -> Any:
    targets = getattr(trade_idea, "take_profits", ())
    if not isinstance(targets, Sequence):
        return NA
    index = target_number - 1
    if index >= len(targets):
        return NA
    return getattr(targets[index], "price", NA)


def _level_field(level: Any, field: str) -> Any:
    return getattr(level, field, NA)


def _short_reason(value: Any) -> str:
    text = " ".join(_display(value).split())
    if len(text) <= 180:
        return text
    return f"{text[:177]}..."


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _int_text(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _seconds_text(value: float) -> str:
    if value == int(value):
        return f"{int(value)} seconds"
    return f"{value:.2f}".rstrip("0").rstrip(".") + " seconds"


__all__ = [
    "DEFAULT_LATEST_RUN_PATH",
    "DEFAULT_WATCH_STATE_PATH",
    "WatchActivation",
    "WatchAlertDelivery",
    "WatchHistoryEntry",
    "WatchIterationSummary",
    "WatchModeError",
    "WatchState",
    "WatchSymbolState",
    "append_watch_output",
    "build_watch_activation_alert_manifest",
    "build_watch_iteration_summary",
    "current_result_is_valid_activation",
    "deliver_watch_activation_alert",
    "format_watch_activation_alert",
    "format_watch_iteration_summary",
    "load_run_payload",
    "load_symbols_from_run",
    "load_watch_state",
    "payload_is_watch_candidate",
    "save_watch_state",
    "seed_watch_state_from_run_payload",
    "should_trigger_activation_alert",
    "state_watch_symbols",
    "symbols_from_run_payload",
    "update_watch_state_for_result",
]
