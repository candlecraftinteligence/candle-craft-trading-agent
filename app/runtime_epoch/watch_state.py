"""Epoch-specific watch-state location. Canonical legacy JSON is never overwritten."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.data.dtos import NA
from app.runtime_epoch.errors import RuntimeEpochConfigurationError
from app.runtime_epoch.models import RuntimeEpochRecord

CANONICAL_LEGACY_WATCH_STATE_NAME = "watch_state.json"
OPERATIONAL_WATCH_STATE_NAME = "watch_state.json"


def canonical_legacy_watch_state_path(base_dir: Path | str = Path("scan_runs")) -> Path:
    return Path(base_dir) / CANONICAL_LEGACY_WATCH_STATE_NAME


def operational_watch_state_path(
    epoch_id: str,
    *,
    base_dir: Path | str = Path("scan_runs"),
) -> Path:
    normalized = str(epoch_id).strip()
    if not normalized:
        raise RuntimeEpochConfigurationError("Operational watch state requires a runtime epoch id.")
    return Path(base_dir) / "epochs" / normalized / OPERATIONAL_WATCH_STATE_NAME


def is_canonical_legacy_watch_state_path(path: Path | str, *, base_dir: Path | str = Path("scan_runs")) -> bool:
    try:
        return Path(path).resolve() == canonical_legacy_watch_state_path(base_dir).resolve()
    except OSError:
        return Path(path) == canonical_legacy_watch_state_path(base_dir)


def _epoch_id(epoch: RuntimeEpochRecord | str) -> str:
    return epoch if isinstance(epoch, str) else epoch.epoch_id


def require_operational_watch_payload(
    payload: Any,
    epoch: RuntimeEpochRecord | str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeEpochConfigurationError("Operational watch state must be a JSON object.")
    raw_epoch = payload.get("runtime_epoch_id")
    if raw_epoch is None:
        raise RuntimeEpochConfigurationError(
            "Operational watch state is missing runtime_epoch_id; refusing to adopt untagged state."
        )
    payload_epoch = str(raw_epoch).strip()
    if not payload_epoch:
        raise RuntimeEpochConfigurationError(
            "Operational watch state runtime_epoch_id is empty; refusing to adopt untagged state."
        )
    if payload_epoch != _epoch_id(epoch):
        raise RuntimeEpochConfigurationError(
            "Operational watch state epoch does not match the active runtime epoch."
        )
    return payload


def load_or_initialize_operational_watch_payload(
    path: Path,
    epoch: RuntimeEpochRecord | str,
    *,
    base_dir: Path | str = Path("scan_runs"),
) -> dict[str, Any]:
    refuse_legacy_watch_fallback(path, epoch, base_dir=base_dir)
    if not path.exists():
        return stamp_watch_payload({}, epoch)
    payload = read_json_object(path)
    if payload is None:
        return stamp_watch_payload({}, epoch)
    return require_operational_watch_payload(payload, epoch)


def stamp_watch_payload(payload: dict[str, Any], epoch: RuntimeEpochRecord | str) -> dict[str, Any]:
    stamped = dict(payload)
    stamped["runtime_epoch_id"] = _epoch_id(epoch)
    return stamped


def read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeEpochConfigurationError(f"Operational watch state is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeEpochConfigurationError(f"Operational watch state is not a JSON object: {path}")
    return payload


def refuse_legacy_watch_fallback(
    path: Path,
    epoch: RuntimeEpochRecord | str,
    *,
    base_dir: Path | str = Path("scan_runs"),
) -> None:
    if is_canonical_legacy_watch_state_path(path, base_dir=base_dir):
        raise RuntimeEpochConfigurationError(
            "Refusing to load or overwrite canonical legacy watch_state.json for operational use."
        )
    expected = operational_watch_state_path(_epoch_id(epoch), base_dir=base_dir)
    try:
        same = path.resolve() == expected.resolve()
    except OSError:
        same = path == expected
    if not same:
        raise RuntimeEpochConfigurationError(
            "Operational watch state path is not the epoch-specific working location."
        )
