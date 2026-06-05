from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.data.dtos import NA
from app.formatters.telegram_signal_detail import TelegramSignalDetail, lifecycle_chain_text
from app.formatters.telegram_signal_formatter import PUBLIC_STATUS_BY_ALERT_TYPE, TelegramAlertType, safe_invalidation_text
from app.telegram_admin.active_watchlists import (
    UNVERIFIED,
    _SIGNAL_CONFIRMED_TYPE,
    _SIGNAL_QUERY_TYPES,
    _TERMINAL_OUTCOME_TYPES,
    _clean,
    _connect_readonly,
    _first_non_na,
    _json_mapping,
    _latest_runtime_database,
    _levels_for_watchlist,
    _public_alert_quality_passes,
    _row_id,
    _select_or_na,
    _sent_alert_attempt_rows,
    _status_key,
    _symbol_result_for_attempt,
    _table_columns,
    _table_exists,
)


@dataclass(frozen=True)
class SignalDetailQueryResult:
    source_available: bool
    detail: TelegramSignalDetail | None = None


def load_active_signal_detail(
    *,
    project_root: Path | str,
    database_path: Path | str,
    selector: str,
) -> SignalDetailQueryResult:
    """Load one active confirmed signal detail from persisted runtime data only."""

    root = Path(project_root)
    preferred_path = _resolve_project_path(root, database_path)
    selected_path = _latest_runtime_database(root, preferred_path, alert_types=_SIGNAL_QUERY_TYPES)
    if selected_path is None:
        return SignalDetailQueryResult(source_available=False)

    try:
        with _connect_readonly(selected_path) as connection:
            rows = _sent_alert_attempt_rows(connection, alert_types=_SIGNAL_QUERY_TYPES)
            detail = _detail_from_rows(connection, rows, selector=selector)
    except (OSError, sqlite3.Error):
        return SignalDetailQueryResult(source_available=False)

    return SignalDetailQueryResult(source_available=True, detail=detail)


def _detail_from_rows(
    connection: sqlite3.Connection,
    rows: Sequence[Mapping[str, Any]],
    *,
    selector: str,
) -> TelegramSignalDetail | None:
    selected = _selector_key(selector)
    if not selected:
        return None

    by_signal: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        signal_id = _clean(row.get("signal_id"))
        if signal_id != NA:
            by_signal.setdefault(signal_id, []).append(row)

    details: list[tuple[int, TelegramSignalDetail]] = []
    for signal_id, signal_rows in by_signal.items():
        confirmed_rows = [row for row in signal_rows if _clean(row.get("alert_type")) == _SIGNAL_CONFIRMED_TYPE]
        if not confirmed_rows:
            continue
        signal_row = max(confirmed_rows, key=_row_id)
        outcome_rows = [row for row in signal_rows if _clean(row.get("alert_type")) != _SIGNAL_CONFIRMED_TYPE]
        if any(_clean(row.get("alert_type")) in _TERMINAL_OUTCOME_TYPES for row in outcome_rows):
            continue
        if not _public_alert_quality_passes(signal_row):
            continue
        if not _row_matches_selector(signal_id, signal_row, selected):
            continue
        latest_row = max((signal_row, *outcome_rows), key=_row_id)
        details.append((_row_id(latest_row), _detail_from_group(connection, signal_id, signal_row, outcome_rows, latest_row)))

    if not details:
        return None
    return max(details, key=lambda item: item[0])[1]


def _detail_from_group(
    connection: sqlite3.Connection,
    signal_id: str,
    signal_row: Mapping[str, Any],
    outcome_rows: Sequence[Mapping[str, Any]],
    latest_row: Mapping[str, Any],
) -> TelegramSignalDetail:
    symbol_row = _symbol_result_for_attempt(connection, latest_row)
    raw_result = _json_mapping(symbol_row.get("raw_result_json"))
    candidate = _candidate_detail(connection, signal_row)
    candidate_raw = _json_mapping(candidate.get("raw_candidate_json"))
    lifecycle_row = _lifecycle_row(connection, signal_id, latest_row)
    lifecycle_events = _lifecycle_events(connection, _first_text(lifecycle_row.get("lifecycle_id"), signal_id))
    levels = _levels_for_watchlist(connection, signal_row, outcome_rows)

    trade_idea = _mapping(raw_result.get("trade_idea"))
    confirmed_facts = _confirmed_facts(trade_idea, raw_result, candidate_raw)
    confirmed_gates = _confirmed_gates(trade_idea, raw_result, lifecycle_row, signal_row)
    lifecycle_chain = _lifecycle_chain(lifecycle_events, raw_result, signal_row, outcome_rows, lifecycle_row)
    lifecycle_reason = _latest_lifecycle_reason(lifecycle_events, lifecycle_row)

    return TelegramSignalDetail(
        symbol=_first_text(signal_row.get("symbol"), raw_result.get("symbol")),
        bias=_first_text(signal_row.get("direction"), trade_idea.get("direction"), candidate.get("direction")),
        status=_status_for_latest_row(latest_row),
        quality=_quality_text(signal_row, raw_result, candidate, candidate_raw),
        lifecycle=lifecycle_chain,
        entry_low=levels.get("entry_low", NA),
        entry_high=levels.get("entry_high", NA),
        stop_loss=levels.get("stop_loss", NA),
        tp1=levels.get("tp1", NA),
        tp2=levels.get("tp2", NA),
        tp3=levels.get("tp3", NA),
        why_it_matters=_why_it_matters(
            trade_idea=trade_idea,
            raw_result=raw_result,
            candidate_raw=candidate_raw,
            symbol_row=symbol_row,
            lifecycle_reason=lifecycle_reason,
            confirmed_facts=confirmed_facts,
        ),
        invalid_if=_invalid_if(
            trade_idea=trade_idea,
            raw_result=raw_result,
            candidate=candidate,
            candidate_raw=candidate_raw,
            lifecycle_row=lifecycle_row,
            direction=_first_text(signal_row.get("direction"), trade_idea.get("direction"), candidate.get("direction")),
            stop_loss=levels.get("stop_loss", NA),
        ),
        confirmed_facts=confirmed_facts,
        confirmed_gates=confirmed_gates,
        lifecycle_reason=lifecycle_reason,
    )


def _candidate_detail(connection: sqlite3.Connection, row: Mapping[str, Any]) -> Mapping[str, Any]:
    if not _table_exists(connection, "setup_candidates"):
        return {}
    columns = _table_columns(connection, "setup_candidates")
    if not {"symbol", "direction"} <= columns:
        return {}
    select_columns = [
        _select_or_na("id", columns),
        _select_or_na("run_id", columns),
        "symbol",
        "direction",
        _select_or_na("entry", columns),
        _select_or_na("stop", columns),
        _select_or_na("tp1", columns),
        _select_or_na("tp2", columns),
        _select_or_na("tp3", columns),
        _select_or_na("invalidation", columns),
        _select_or_na("quality_grade", columns),
        _select_or_na("risk_warning", columns),
        _select_or_na("raw_candidate_json", columns),
    ]
    symbol = _clean(row.get("symbol"))
    direction = _clean(row.get("direction"))
    if symbol == NA or direction == NA:
        return {}
    scan_run_id = _clean(row.get("scan_run_id"))
    if "run_id" in columns and scan_run_id != NA:
        order_clause = "ORDER BY CASE WHEN run_id = ? THEN 0 ELSE 1 END, id DESC"
        params: tuple[Any, ...] = (symbol, direction, scan_run_id)
    else:
        order_clause = "ORDER BY id DESC" if "id" in columns else ""
        params = (symbol, direction)
    candidate = connection.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM setup_candidates
        WHERE UPPER(symbol) = UPPER(?)
          AND UPPER(direction) = UPPER(?)
        {order_clause}
        LIMIT 1
        """,
        params,
    ).fetchone()
    return dict(candidate) if candidate is not None else {}


def _lifecycle_row(connection: sqlite3.Connection, signal_id: str, row: Mapping[str, Any]) -> Mapping[str, Any]:
    if not _table_exists(connection, "setup_lifecycle_records"):
        return {}
    columns = _table_columns(connection, "setup_lifecycle_records")
    if not {"lifecycle_id", "symbol"} <= columns:
        return {}
    select_columns = [
        "lifecycle_id",
        "symbol",
        _select_or_na("mode", columns),
        _select_or_na("direction", columns),
        _select_or_na("current_state", columns),
        _select_or_na("last_seen_at", columns),
        _select_or_na("last_transition_at", columns),
        _select_or_na("failed_gate", columns),
        _select_or_na("readiness_score", columns),
        _select_or_na("quality_score", columns),
        _select_or_na("action_label", columns),
        _select_or_na("invalidation_reason", columns),
    ]
    if _clean(signal_id) != NA:
        record = connection.execute(
            f"""
            SELECT {", ".join(select_columns)}
            FROM setup_lifecycle_records
            WHERE lifecycle_id = ?
            LIMIT 1
            """,
            (signal_id,),
        ).fetchone()
        if record is not None:
            return dict(record)

    symbol = _clean(row.get("symbol"))
    direction = _clean(row.get("direction"))
    if symbol == NA:
        return {}
    if "direction" in columns and direction != NA:
        record = connection.execute(
            f"""
            SELECT {", ".join(select_columns)}
            FROM setup_lifecycle_records
            WHERE UPPER(symbol) = UPPER(?)
              AND UPPER(direction) = UPPER(?)
            ORDER BY last_seen_at DESC
            LIMIT 1
            """,
            (symbol, direction),
        ).fetchone()
    else:
        record = connection.execute(
            f"""
            SELECT {", ".join(select_columns)}
            FROM setup_lifecycle_records
            WHERE UPPER(symbol) = UPPER(?)
            ORDER BY last_seen_at DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
    return dict(record) if record is not None else {}


def _lifecycle_events(connection: sqlite3.Connection, lifecycle_id: str) -> tuple[Mapping[str, Any], ...]:
    if _clean(lifecycle_id) == NA or not _table_exists(connection, "setup_lifecycle_events"):
        return ()
    columns = _table_columns(connection, "setup_lifecycle_events")
    if "lifecycle_id" not in columns:
        return ()
    select_columns = [
        _select_or_na("event_id", columns),
        "lifecycle_id",
        _select_or_na("timestamp", columns),
        _select_or_na("from_state", columns),
        _select_or_na("to_state", columns),
        _select_or_na("reason", columns),
        _select_or_na("failed_gate", columns),
        _select_or_na("notes", columns),
    ]
    rows = connection.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM setup_lifecycle_events
        WHERE lifecycle_id = ?
        ORDER BY timestamp ASC, event_id ASC
        """,
        (lifecycle_id,),
    ).fetchall()
    return tuple(dict(row) for row in rows)


def _lifecycle_chain(
    events: Sequence[Mapping[str, Any]],
    raw_result: Mapping[str, Any],
    signal_row: Mapping[str, Any],
    outcome_rows: Sequence[Mapping[str, Any]],
    lifecycle_row: Mapping[str, Any],
) -> str:
    event_states: list[Any] = []
    if events:
        first_from = _clean(events[0].get("from_state"))
        if first_from != NA:
            event_states.append(first_from)
        event_states.extend(event.get("to_state") for event in events)
        chain = lifecycle_chain_text(event_states)
        if chain != NA:
            return chain

    chain = lifecycle_chain_text(_sequence(raw_result.get("status_history")))
    if chain != NA:
        return chain

    alert_states = [
        _first_text(row.get("new_state"), row.get("lifecycle_state"))
        for row in sorted((signal_row, *outcome_rows), key=_row_id)
    ]
    chain = lifecycle_chain_text(alert_states)
    if chain != NA:
        return chain
    return _first_text(lifecycle_row.get("current_state"), signal_row.get("lifecycle_state"), signal_row.get("new_state"))


def _latest_lifecycle_reason(events: Sequence[Mapping[str, Any]], lifecycle_row: Mapping[str, Any]) -> str:
    if events:
        latest = events[-1]
        return _first_public_text(latest.get("notes"), latest.get("reason"), latest.get("failed_gate"))
    return _first_public_text(lifecycle_row.get("invalidation_reason"), lifecycle_row.get("failed_gate"), lifecycle_row.get("action_label"))


def _why_it_matters(
    *,
    trade_idea: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    candidate_raw: Mapping[str, Any],
    symbol_row: Mapping[str, Any],
    lifecycle_reason: str,
    confirmed_facts: Sequence[str],
) -> str:
    return _first_public_text(
        trade_idea.get("reason_for_trade"),
        raw_result.get("reason_for_trade"),
        candidate_raw.get("reason_for_trade"),
        _facts_sentence(confirmed_facts),
        _diagnostic_summary(raw_result),
        lifecycle_reason,
        symbol_row.get("rejection_reason"),
        symbol_row.get("next_trigger_needed"),
        raw_result.get("short_reason"),
        raw_result.get("display_reason"),
    )


def _invalid_if(
    *,
    trade_idea: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    candidate: Mapping[str, Any],
    candidate_raw: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
    direction: str,
    stop_loss: str,
) -> str:
    explicit = _first_public_text(
        trade_idea.get("invalidation"),
        raw_result.get("invalidation"),
        candidate.get("invalidation"),
        candidate_raw.get("invalidation"),
        trade_idea.get("cancel_condition"),
        candidate_raw.get("cancel_condition"),
        lifecycle_row.get("invalidation_reason"),
    )
    if explicit != NA:
        return explicit
    return _stop_based_invalidation(direction=direction, stop_loss=stop_loss)


def _stop_based_invalidation(*, direction: str, stop_loss: str) -> str:
    message = safe_invalidation_text(
        TelegramSignalDetailShim(direction=direction, stop_loss=stop_loss)
    )
    return _first_public_text(message)


@dataclass(frozen=True)
class TelegramSignalDetailShim:
    direction: Any = NA
    stop_loss: Any = NA
    invalidation_reason: Any = NA
    watchlist_invalidation_reason: Any = NA


def _quality_text(
    signal_row: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    candidate: Mapping[str, Any],
    candidate_raw: Mapping[str, Any],
) -> str:
    setup_quality = _mapping(raw_result.get("setup_quality"))
    return _first_text(
        _enum_text(setup_quality.get("quality_grade")),
        _enum_text(raw_result.get("quality_grade")),
        _enum_text(_mapping(raw_result.get("trade_idea")).get("grade")),
        candidate.get("quality_grade"),
        candidate_raw.get("quality_grade"),
        signal_row.get("setup_quality_score"),
    )


def _status_for_latest_row(row: Mapping[str, Any]) -> str:
    alert_type = _clean(row.get("alert_type"))
    try:
        public_status = PUBLIC_STATUS_BY_ALERT_TYPE[TelegramAlertType(alert_type)]
    except (ValueError, KeyError):
        public_status = NA
    return _first_text(public_status, row.get("new_state"), row.get("lifecycle_state"))


def _confirmed_facts(*sources: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for source in sources:
        for key in ("confirmed_facts", "facts", "confirmation_facts"):
            values.extend(_public_sequence(source.get(key)))
    return tuple(dict.fromkeys(values))


def _confirmed_gates(
    trade_idea: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
    signal_row: Mapping[str, Any],
) -> tuple[str, ...]:
    gates: list[str] = []
    quality_gate = _mapping(trade_idea.get("quality_gate_result"))
    if quality_gate.get("passed") is True:
        gates.append("Quality gate passed.")
    setup_quality = _mapping(raw_result.get("setup_quality"))
    decision_reason = _first_public_text(setup_quality.get("decision_reason"))
    if decision_reason != NA:
        gates.append(decision_reason)
    state = _first_text(lifecycle_row.get("current_state"), signal_row.get("lifecycle_state"), signal_row.get("new_state"))
    if state != NA:
        gates.append(f"Lifecycle state {state}.")
    return tuple(dict.fromkeys(gates))


def _diagnostic_summary(raw_result: Mapping[str, Any]) -> str:
    diagnostics = raw_result.get("strategy_diagnostics")
    if not isinstance(diagnostics, Mapping):
        return NA
    for value in diagnostics.values():
        if not isinstance(value, Mapping):
            continue
        text = _first_public_text(
            value.get("reason_for_trade"),
            value.get("structure_reason"),
            value.get("confirmation_reason"),
            value.get("confluence"),
            value.get("summary"),
        )
        if text != NA:
            return text
    return NA


def _facts_sentence(values: Sequence[Any]) -> str:
    facts = [_first_public_text(value).rstrip(".") for value in values if _first_public_text(value) != NA]
    if not facts:
        return NA
    return ". ".join(facts[:3]) + "."


def _first_public_text(*values: Any) -> str:
    for value in values:
        text = _public_text(value)
        if text != NA:
            return text
    return NA


def _public_sequence(value: Any) -> list[str]:
    values = _sequence(value)
    return [text for text in (_public_text(item) for item in values) if text != NA]


def _public_text(value: Any) -> str:
    text = _first_text(_enum_text(value))
    if text == NA:
        return NA
    if _is_unverified(text):
        return UNVERIFIED
    lowered = text.lower()
    if (
        "decimal(" in lowered
        or "strategy_diagnostics" in lowered
        or "raw_result" in lowered
        or "first_failed_gate" in lowered
        or "failed_gate" in lowered
        or "{" in text
        or "}" in text
        or lowered in {"true", "false"}
    ):
        return NA
    cleaned = " ".join(text.split()).strip()
    if not cleaned:
        return NA
    return cleaned if len(cleaned) <= 180 else f"{cleaned[:177].rstrip()}..."


def _row_matches_selector(signal_id: str, row: Mapping[str, Any], selected: str) -> bool:
    return selected in {
        _selector_key(signal_id),
        _selector_key(row.get("symbol")),
    }


def _selector_key(value: Any) -> str:
    text = _clean(value)
    if text == NA:
        return ""
    return text.strip().upper()


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, Mapping)):
        return tuple(value)
    if _clean(value) != NA:
        return (value,)
    return ()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _enum_text(value: Any) -> Any:
    enum_value = getattr(value, "value", None)
    return enum_value if isinstance(enum_value, str) else value


def _first_text(*values: Any) -> str:
    return _clean(_first_non_na(*values))


def _is_unverified(value: Any) -> bool:
    return "unverified" in _status_key(value)


def _resolve_project_path(project_root: Path, value: Path | str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


__all__ = [
    "SignalDetailQueryResult",
    "load_active_signal_detail",
]
