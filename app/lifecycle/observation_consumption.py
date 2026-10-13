"""Prospective scan-decision receipts, stored on the validated operational origin.

An origin is run/symbol scoped, not mode scoped. Each mode binds once to its
first lifecycle owner. Direction and structural lineage are decision content,
so changing either cannot turn a retry into a new contribution. This is not a
closed-bar policy or research admission ledger.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass

from app.lifecycle.state_machine import LifecycleObservation
from app.runtime_epoch.authority import require_active_runtime_epoch
from app.runtime_epoch.errors import RuntimeEpochIdentityCollisionError, RuntimeEpochOriginError
from app.runtime_epoch.models import SymbolOriginDecision
from app.runtime_epoch.origin import load_granted_origin
from app.runtime_epoch.ownership import require_current_epoch_lifecycle_id


@dataclass(frozen=True)
class ObservationConsumption:
    origin_id: str
    mode: str
    digest: str
    document: dict | None
    owner_id: str | None

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
        if (
            not isinstance(document, dict) or set(document) != {"version", "modes"}
            or document["version"] != 1 or not isinstance(document["modes"], dict)
        ):
            raise ValueError("unsupported receipt document")
        receipt = document["modes"].get(mode)
        owner_id = None
        if mode in document["modes"]:
            if not isinstance(receipt, dict) or receipt.get("digest") != digest:
                raise ValueError("conflicting decision content")
            owner_id = receipt["lifecycle_id"]
            owner = require_current_epoch_lifecycle_id(connection, owner_id)
            if owner["mode"] != mode or owner["direction"] != observation.direction.strip().lower():
                raise ValueError("conflicting lifecycle owner")
            if owner["symbol"] != observation.symbol.strip().upper():
                raise ValueError("conflicting lifecycle symbol")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeEpochIdentityCollisionError(
            f"Observation consumption integrity exception: {exc}"
        ) from exc
    return ObservationConsumption(str(row["origin_id"]), mode, digest, document, owner_id)


def persist_consumption(
    connection: sqlite3.Connection,
    consumption: ObservationConsumption,
    *,
    lifecycle_id: str,
    consumed_at: str,
) -> None:
    """Receipt and subsequent owner write share a savepoint and enclosing commit."""
    if not connection.in_transaction or consumption.legacy or consumption.owner_id is not None:
        raise RuntimeEpochOriginError("Cannot persist an unowned or repeated consumption.")
    document = consumption.document
    assert document is not None
    document["modes"][consumption.mode] = {
        "lifecycle_id": lifecycle_id,
        "digest": consumption.digest,
        "consumed_at": consumed_at,
    }
    connection.execute(
        "UPDATE runtime_operational_origins SET lifecycle_consumption_json = ? WHERE origin_id = ?",
        (_json(document), consumption.origin_id),
    )


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
