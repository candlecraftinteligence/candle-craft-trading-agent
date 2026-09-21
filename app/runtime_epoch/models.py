"""Immutable operational epoch, origin, and cohort contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RUNTIME_EPOCH_CONTRACT_VERSION = "cci-runtime-epoch-isolation-v1"
ACTIVE_EPOCH_CONTROL_KEY = "active"
ORIGIN_KIND_LIVE_FRESH = "live_fresh"
ORIGIN_STATUS_GRANTED = "granted"
ORIGIN_STATUS_BLOCKED = "blocked"
RUN_STATUS_REGISTERED = "registered"
COHORT_LEGACY_OR_UNATTRIBUTED = "LEGACY_OR_UNATTRIBUTED"
COHORT_CURRENT_EPOCH_OPERATIONAL = "CURRENT_EPOCH_OPERATIONAL"
LIVE_SCAN_EVALUATION = "live_scan"
RESUMED_PAYLOAD_EVALUATION = "resumed_payload"
WATCH_SEED_EVALUATION = "watch_seed"
CACHED_PRE_EPOCH_EVALUATION = "cached_pre_epoch"
REPLAY_EVALUATION = "replay"
IMPORTED_EVALUATION = "imported"
UNSPECIFIED_EVALUATION = "unspecified"

EvaluationOriginKind = Literal[
    "live_scan",
    "resumed_payload",
    "watch_seed",
    "cached_pre_epoch",
    "replay",
    "imported",
    "unspecified",
]


@dataclass(frozen=True)
class RuntimeEpochIdentity:
    epoch_id: str
    cutoff_at: str
    contract_version: str = RUNTIME_EPOCH_CONTRACT_VERSION
    reviewed_release_sha: str = ""
    generation_binding: str = ""


@dataclass(frozen=True)
class RuntimeEpochRecord:
    epoch_id: str
    contract_version: str
    activated_at: str
    cutoff_at: str
    reviewed_release_sha: str
    generation_binding: str
    created_at: str

    @property
    def identity(self) -> RuntimeEpochIdentity:
        return RuntimeEpochIdentity(
            epoch_id=self.epoch_id,
            cutoff_at=self.cutoff_at,
            contract_version=self.contract_version,
            reviewed_release_sha=self.reviewed_release_sha,
            generation_binding=self.generation_binding,
        )


@dataclass(frozen=True)
class OperationalRunRegistration:
    run_id: str
    runtime_epoch_id: str
    registered_at: str
    status: str = RUN_STATUS_REGISTERED
    producer_started_at: str | None = None


@dataclass(frozen=True)
class SymbolOriginDecision:
    granted: bool
    origin_id: str | None
    run_id: str
    symbol: str
    reason: str
    evaluation_completed_at: str | None = None
    decision_cutoff_at: str | None = None
    producer_observed_at: str | None = None
    origin_kind: str = UNSPECIFIED_EVALUATION


@dataclass(frozen=True)
class PublicOwnershipDecision:
    allowed: bool
    reason: str
    runtime_epoch_id: str | None = None
    origin_lifecycle_id: str | None = None
    mutate: bool = False
    send: bool = False
