"""Prospective scan-decision receipts, stored on the validated operational origin.

An origin is run/symbol scoped, not mode scoped. A mode decision binds once.
Direction and structural lineage are decision content, so changing either cannot
turn a retry into a new contribution.

Version 1 stores one owner under the observation mode. Version 2 keeps that
slot and can also store a source decision whose owners are explicit. A source
slot records the original scanner observation. It may name several owners,
including opposite directions of one mode. Owner mode and direction live on
the binding; they do not replace the source decision. A later observation of a
different mode remains a separate contribution.

Unknown versions, extra document keys, and malformed slots fail closed. A
version 1 document is not reread as a multi-owner receipt. This is not a
closed-bar policy or research admission ledger.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from app.lifecycle.state_machine import LifecycleObservation
from app.runtime_epoch.authority import require_active_runtime_epoch
from app.runtime_epoch.errors import RuntimeEpochIdentityCollisionError, RuntimeEpochOriginError
from app.runtime_epoch.models import SymbolOriginDecision
from app.runtime_epoch.origin import load_granted_origin
from app.runtime_epoch.ownership import require_current_epoch_lifecycle_id

_SINGLE_OWNER_KEYS = frozenset({"lifecycle_id", "digest", "consumed_at"})
_SOURCE_OWNER_KEYS = frozenset({"digest", "consumed_at", "owners"})
_BINDING_KEYS = frozenset({"lifecycle_id", "mode", "direction"})


@dataclass(frozen=True)
class ObservationConsumption:
    origin_id: str
    mode: str
    digest: str
    document: dict | None
    owner_id: str | None
    bound_owners: tuple[tuple[str, str, str], ...] = ()

    @property
    def legacy(self) -> bool:
        return self.document is None


def inspect_consumption(
    connection: sqlite3.Connection,
    *,
    origin: SymbolOriginDecision,
    observation: LifecycleObservation,
    lineage: dict[str, str | None],
) -> ObservationConsumption:
    """Read only inside the service's serialized write transaction, before rotation."""
    if not connection.in_transaction:
        raise RuntimeEpochOriginError("Observation consumption requires an enclosing transaction.")
    epoch = require_active_runtime_epoch(connection)
    row = load_granted_origin(connection, origin.origin_id or "")
    if (
        not origin.granted or row is None
        or row["runtime_epoch_id"] != epoch.epoch_id
        or row["run_id"] != origin.run_id
        or row["symbol"] != observation.symbol.strip().upper()
    ):
        raise RuntimeEpochOriginError("Observation consumption requires matching granted lineage.")
    mode = observation.mode.strip().lower()
    payload = {"version": 1, "observation": asdict(observation), "lineage": lineage}
    digest = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    raw = row["lifecycle_consumption_json"]
    if raw is None:
        return ObservationConsumption(str(row["origin_id"]), mode, digest, None, None)
    try:
        document = json.loads(raw)
        if not isinstance(document, dict) or set(document) != {"version", "modes"} or not isinstance(document["modes"], dict):
            raise ValueError("unsupported receipt document")
        version = document["version"]
        if version == 1:
            return _inspect_version_one(connection, document, row, mode, digest, observation)
        if version == 2:
            return _inspect_version_two(connection, document, row, mode, digest, observation)
        raise ValueError("unsupported receipt document")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeEpochIdentityCollisionError(
            f"Observation consumption integrity exception: {exc}"
        ) from exc


def persist_consumption(
    connection: sqlite3.Connection,
    consumption: ObservationConsumption,
    *,
    lifecycle_id: str,
    consumed_at: str,
) -> None:
    """Receipt and subsequent owner write share a savepoint and enclosing commit."""
    if (
        not connection.in_transaction
        or consumption.legacy
        or consumption.owner_id is not None
        or consumption.bound_owners
    ):
        raise RuntimeEpochOriginError("Cannot persist an unowned or repeated consumption.")
    document = consumption.document
    assert document is not None
    if consumption.mode in document["modes"]:
        raise RuntimeEpochOriginError("Cannot persist an unowned or repeated consumption.")
    document["modes"][consumption.mode] = {
        "lifecycle_id": lifecycle_id,
        "digest": consumption.digest,
        "consumed_at": consumed_at,
    }
    connection.execute(
        "UPDATE runtime_operational_origins SET lifecycle_consumption_json = ? WHERE origin_id = ?",
        (_json(document), consumption.origin_id),
    )


def persist_source_owner_bindings(
    connection: sqlite3.Connection,
    consumption: ObservationConsumption,
    *,
    owners: Sequence[Mapping[str, str]],
    consumed_at: str,
) -> None:
    """Record the original decision once, with every owner it holds.

    An empty version 1 document is replaced by version 2. A version 1 document
    that already names mode decisions is copied forward slot by slot. The
    source slot does not contain a lifecycle id, so it cannot be read as one
    of those mode decisions.
    """

    if (
        not connection.in_transaction
        or consumption.legacy
        or consumption.owner_id is not None
        or consumption.bound_owners
        or not owners
    ):
        raise RuntimeEpochOriginError("Cannot persist an unowned or repeated consumption.")
    document = consumption.document
    assert document is not None
    try:
        upgraded = _upgrade_source_document(connection, document)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeEpochIdentityCollisionError(
            f"Observation consumption integrity exception: {exc}"
        ) from exc
    if consumption.mode in upgraded["modes"]:
        raise RuntimeEpochIdentityCollisionError(
            "Observation consumption integrity exception: conflicting decision content"
        )
    upgraded["modes"][consumption.mode] = {
        "digest": consumption.digest,
        "consumed_at": consumed_at,
        "owners": _validated_owner_bindings(connection, owners),
    }
    connection.execute(
        "UPDATE runtime_operational_origins SET lifecycle_consumption_json = ? WHERE origin_id = ?",
        (_json(upgraded), consumption.origin_id),
    )


def _inspect_version_one(
    connection: sqlite3.Connection,
    document: dict,
    row: sqlite3.Row,
    mode: str,
    digest: str,
    observation: LifecycleObservation,
) -> ObservationConsumption:
    if document["version"] != 1:
        raise ValueError("unsupported receipt document")
    receipt = document["modes"].get(mode)
    owner_id = None
    if mode in document["modes"]:
        if not isinstance(receipt, dict) or set(receipt) != _SINGLE_OWNER_KEYS or receipt.get("digest") != digest:
            if not isinstance(receipt, dict) or receipt.get("digest") != digest:
                raise ValueError("conflicting decision content")
            raise ValueError("unsupported receipt document")
        owner_id = receipt["lifecycle_id"]
        _require_mode_owner(connection, owner_id, mode, observation)
    return ObservationConsumption(str(row["origin_id"]), mode, digest, document, owner_id)


def _inspect_version_two(
    connection: sqlite3.Connection,
    document: dict,
    row: sqlite3.Row,
    mode: str,
    digest: str,
    observation: LifecycleObservation,
) -> ObservationConsumption:
    if document["version"] != 2:
        raise ValueError("unsupported receipt document")
    if mode not in document["modes"]:
        return ObservationConsumption(str(row["origin_id"]), mode, digest, document, None)
    receipt = document["modes"][mode]
    if not isinstance(receipt, dict) or receipt.get("digest") != digest:
        raise ValueError("conflicting decision content")
    if "lifecycle_id" in receipt:
        if set(receipt) not in {_SINGLE_OWNER_KEYS, _SINGLE_OWNER_KEYS | {"owners"}}:
            raise ValueError("unsupported receipt document")
        owner_id = receipt["lifecycle_id"]
        owner = _require_mode_owner(connection, owner_id, mode, observation)
        if "owners" in receipt:
            _require_single_owner_list(receipt["owners"], owner_id, mode, owner["direction"])
        return ObservationConsumption(str(row["origin_id"]), mode, digest, document, str(owner_id))
    if set(receipt) != _SOURCE_OWNER_KEYS:
        raise ValueError("unsupported receipt document")
    bindings = _source_bindings(connection, receipt["owners"], observation)
    return ObservationConsumption(
        str(row["origin_id"]),
        mode,
        digest,
        document,
        None,
        bindings,
    )


def _require_mode_owner(
    connection: sqlite3.Connection,
    lifecycle_id: str,
    mode: str,
    observation: LifecycleObservation,
) -> sqlite3.Row:
    owner = require_current_epoch_lifecycle_id(connection, lifecycle_id)
    if owner["mode"] != mode or owner["direction"] != observation.direction.strip().lower():
        raise ValueError("conflicting lifecycle owner")
    if owner["symbol"] != observation.symbol.strip().upper():
        raise ValueError("conflicting lifecycle symbol")
    return owner


def _require_single_owner_list(owners: object, lifecycle_id: str, mode: str, direction: str) -> None:
    if not isinstance(owners, list) or len(owners) != 1 or not isinstance(owners[0], dict):
        raise ValueError("unsupported receipt document")
    item = owners[0]
    if set(item) != _BINDING_KEYS:
        raise ValueError("unsupported receipt document")
    if str(item["lifecycle_id"]) != str(lifecycle_id) or str(item["mode"]).strip().lower() != mode:
        raise ValueError("conflicting lifecycle owner")
    if str(item["direction"]).strip().lower() != str(direction).strip().lower():
        raise ValueError("conflicting lifecycle owner")


def _source_bindings(
    connection: sqlite3.Connection,
    owners: object,
    observation: LifecycleObservation,
) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(owners, list) or not owners:
        raise ValueError("unsupported receipt document")
    symbol = observation.symbol.strip().upper()
    bindings: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in owners:
        if not isinstance(item, dict) or set(item) != _BINDING_KEYS:
            raise ValueError("unsupported receipt document")
        lifecycle_id = str(item["lifecycle_id"]).strip()
        mode = str(item["mode"]).strip().lower()
        direction = str(item["direction"]).strip().lower()
        if not lifecycle_id or lifecycle_id in seen or not mode or not direction:
            raise ValueError("unsupported receipt document")
        seen.add(lifecycle_id)
        owner = require_current_epoch_lifecycle_id(connection, lifecycle_id)
        if owner["mode"] != mode or owner["direction"] != direction:
            raise ValueError("conflicting lifecycle owner")
        if owner["symbol"] != symbol:
            raise ValueError("conflicting lifecycle symbol")
        bindings.append((lifecycle_id, mode, direction))
    return tuple(bindings)


def _validated_owner_bindings(
    connection: sqlite3.Connection,
    owners: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for owner in owners:
        if set(owner) != _BINDING_KEYS:
            raise ValueError("unsupported receipt document")
        lifecycle_id = str(owner["lifecycle_id"]).strip()
        mode = str(owner["mode"]).strip().lower()
        direction = str(owner["direction"]).strip().lower()
        if not lifecycle_id or lifecycle_id in seen or not mode or not direction:
            raise ValueError("unsupported receipt document")
        seen.add(lifecycle_id)
        row = require_current_epoch_lifecycle_id(connection, lifecycle_id)
        if row["mode"] != mode or row["direction"] != direction:
            raise ValueError("conflicting lifecycle owner")
        normalized.append({"lifecycle_id": lifecycle_id, "mode": mode, "direction": direction})
    normalized.sort(key=lambda item: (item["mode"], item["direction"], item["lifecycle_id"]))
    return normalized


def _upgrade_source_document(connection: sqlite3.Connection, document: dict) -> dict:
    if set(document) != {"version", "modes"} or not isinstance(document["modes"], dict):
        raise ValueError("unsupported receipt document")
    version = document["version"]
    if version == 2:
        return {"version": 2, "modes": dict(document["modes"])}
    if version != 1:
        raise ValueError("unsupported receipt document")
    modes: dict[str, dict] = {}
    for mode, slot in document["modes"].items():
        slot_mode = str(mode).strip().lower()
        if not isinstance(slot, dict) or set(slot) != _SINGLE_OWNER_KEYS:
            raise ValueError("unsupported receipt document")
        owner = require_current_epoch_lifecycle_id(connection, slot["lifecycle_id"])
        if owner["mode"] != slot_mode:
            raise ValueError("conflicting lifecycle owner")
        modes[slot_mode] = {
            "lifecycle_id": str(slot["lifecycle_id"]),
            "digest": slot["digest"],
            "consumed_at": slot["consumed_at"],
            "owners": [
                {
                    "lifecycle_id": str(slot["lifecycle_id"]),
                    "mode": owner["mode"],
                    "direction": owner["direction"],
                }
            ],
        }
    return {"version": 2, "modes": modes}


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
