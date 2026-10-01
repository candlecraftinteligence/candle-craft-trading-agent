"""Pre-invocation dependency closure for one lifecycle outcome call.

The snapshot is the rows the evaluator and its repository writes actually
read. It does not copy the operational database, other symbols, or secrets.
Absence and conflicting lineage are stored as observed. Replay seeding inserts
only these rows.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.lifecycle.models import SetupLifecycleRecord, SetupTransitionResult
from app.research.durable_source_replay.bounds import BoundExceeded, BoundLimits
from app.research.durable_source_replay.codec import CodecError, _tag
from app.research.durable_source_replay.constants import (
    NONSEMANTIC_EVENT_COLUMNS,
    NONSEMANTIC_PROGRESS_COLUMNS,
)


def snapshot_dependency_closure(
    connection: sqlite3.Connection,
    *,
    lifecycle_id: str,
    scan_run_id: str | None,
    limits: BoundLimits,
) -> dict[str, Any]:
    lifecycle = _one(
        connection,
        "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (lifecycle_id,),
    )
    progress = _all(
        connection,
        """
        SELECT * FROM setup_lifecycle_outcome_progress
        WHERE lifecycle_id = ?
        ORDER BY id ASC
        """,
        (lifecycle_id,),
    )
    events = _all(
        connection,
        """
        SELECT * FROM setup_lifecycle_events
        WHERE lifecycle_id = ?
        ORDER BY timestamp ASC, event_id ASC
        """,
        (lifecycle_id,),
    )
    if len(progress) > limits.max_progress_rows:
        raise BoundExceeded("progress_row_limit")
    if len(events) > limits.max_events:
        raise BoundExceeded("event_row_limit")
    control = _one(
        connection,
        "SELECT * FROM runtime_epoch_control WHERE control_key = 'active'",
        (),
    )
    epoch = None
    if control is not None and control.get("epoch_id"):
        epoch = _one(
            connection,
            "SELECT * FROM runtime_epochs WHERE epoch_id = ?",
            (str(control["epoch_id"]),),
        )
    origin_id = None
    if lifecycle is not None:
        raw_origin = lifecycle.get("creation_origin_id")
        if raw_origin is not None and str(raw_origin).strip():
            origin_id = str(raw_origin)
    origin = None
    if origin_id is not None:
        origin = _one(
            connection,
            "SELECT * FROM runtime_operational_origins WHERE origin_id = ?",
            (origin_id,),
        )
    run_ids: list[str] = []
    if origin is not None and origin.get("run_id"):
        run_ids.append(str(origin["run_id"]))
    if scan_run_id is not None and str(scan_run_id).strip():
        text = str(scan_run_id).strip()
        if text not in run_ids:
            run_ids.append(text)
    runs = [
        {
            "run_id": run_id,
            "row": _one(
                connection,
                "SELECT * FROM runtime_operational_runs WHERE run_id = ?",
                (run_id,),
            ),
        }
        for run_id in run_ids
    ]
    return {
        "lifecycle_id": lifecycle_id,
        "lifecycle_row": lifecycle,
        "progress_rows": progress,
        "event_rows": events,
        "runtime_epoch_control": control,
        "runtime_epoch": epoch,
        "origin_id_observed": origin_id,
        "origin_row": origin,
        "run_rows": runs,
    }


def snapshot_effects(connection: sqlite3.Connection, *, lifecycle_id: str) -> dict[str, Any]:
    return {
        "lifecycle_id": lifecycle_id,
        "lifecycle_row": _one(
            connection,
            "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
            (lifecycle_id,),
        ),
        "progress_rows": _all(
            connection,
            """
            SELECT * FROM setup_lifecycle_outcome_progress
            WHERE lifecycle_id = ?
            ORDER BY id ASC
            """,
            (lifecycle_id,),
        ),
        "event_rows": _all(
            connection,
            """
            SELECT * FROM setup_lifecycle_events
            WHERE lifecycle_id = ?
            ORDER BY timestamp ASC, event_id ASC
            """,
            (lifecycle_id,),
        ),
    }


def semantic_effects(effects: dict[str, Any]) -> dict[str, Any]:
    progress = []
    for row in effects.get("progress_rows") or []:
        progress.append(
            {
                key: value
                for key, value in row.items()
                if key not in NONSEMANTIC_PROGRESS_COLUMNS
            }
        )
    progress.sort(key=lambda item: (str(item.get("plan_identity")), str(item.get("lifecycle_id"))))
    events = []
    for ordinal, row in enumerate(effects.get("event_rows") or []):
        semantic = {
            key: value
            for key, value in row.items()
            if key not in NONSEMANTIC_EVENT_COLUMNS
        }
        semantic["ordinal"] = ordinal
        events.append(semantic)
    return {
        "lifecycle_row": effects.get("lifecycle_row"),
        "progress_rows": progress,
        "event_rows": events,
    }


def project_evaluation(result: Any) -> dict[str, Any]:
    return {
        "processed_candles": int(result.processed_candles),
        "record": _model_dict(result.record),
        "progress": None if result.progress is None else _model_dict(result.progress),
        "transitions": [_transition_dict(item) for item in result.transitions],
        "last_transition": None
        if result.last_transition is None
        else _transition_dict(result.last_transition),
    }


def project_exception(exc: BaseException) -> dict[str, Any]:
    message = str(exc)
    return {
        "exception_class": type(exc).__name__,
        "exception_message": message,
    }


def seed_prestate(connection: sqlite3.Connection, prestate: dict[str, Any]) -> None:
    """Insert captured rows only. Do not grant an origin or activate an epoch."""

    epoch = prestate.get("runtime_epoch")
    if isinstance(epoch, dict):
        _insert_row(connection, "runtime_epochs", epoch)
    control = prestate.get("runtime_epoch_control")
    if isinstance(control, dict):
        _insert_row(connection, "runtime_epoch_control", control)
    for item in prestate.get("run_rows") or []:
        row = item.get("row") if isinstance(item, dict) else None
        if isinstance(row, dict):
            _insert_row(connection, "runtime_operational_runs", row)
    origin = prestate.get("origin_row")
    if isinstance(origin, dict):
        _insert_row(connection, "runtime_operational_origins", origin)
    lifecycle = prestate.get("lifecycle_row")
    if isinstance(lifecycle, dict):
        _insert_row(connection, "setup_lifecycle_records", lifecycle)
    for row in prestate.get("progress_rows") or []:
        _insert_row(connection, "setup_lifecycle_outcome_progress", row)
    for row in prestate.get("event_rows") or []:
        _insert_row(connection, "setup_lifecycle_events", row)


def reconstruct_record(payload: dict[str, Any]) -> SetupLifecycleRecord:
    return SetupLifecycleRecord(**_restore_model_fields(SetupLifecycleRecord, payload))


def _model_dict(model: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in model.__class__.model_fields:
        values[name] = _jsonable(getattr(model, name))
    return values


def _transition_dict(transition: SetupTransitionResult) -> dict[str, Any]:
    return {
        "lifecycle_id": transition.lifecycle_id,
        "symbol": transition.symbol,
        "from_state": None if transition.from_state is None else transition.from_state.value,
        "to_state": transition.to_state.value,
        "reason": transition.reason.value,
        "transitioned": bool(transition.transitioned),
        "allowed": bool(transition.allowed),
        "notes": transition.notes,
        "event": None if transition.event is None else _model_dict(transition.event),
        "record": None if transition.record is None else _model_dict(transition.record),
    }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "value") and hasattr(value, "__class__") and value.__class__.__module__.startswith("app."):
        return value.value
    return value


def _restore_model_fields(model_type: type[Any], payload: dict[str, Any]) -> dict[str, Any]:
    restored: dict[str, Any] = {}
    for name in model_type.model_fields:
        if name not in payload:
            continue
        restored[name] = payload[name]
    return restored


def _insert_row(connection: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    columns = [
        str(item[1])
        for item in connection.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    present = [column for column in columns if column in row]
    if not present:
        raise CodecError(f"seed_row_empty:{table}")
    quoted = ", ".join(_ident(column) for column in present)
    placeholders = ", ".join("?" for _ in present)
    connection.execute(
        f"INSERT INTO {_ident(table)} ({quoted}) VALUES ({placeholders})",
        tuple(row[column] for column in present),
    )


def _ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _one(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = connection.execute(sql, params).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _all(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    rows = connection.execute(sql, params).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def tag_tree(value: Any) -> Any:
    return _tag(value)
