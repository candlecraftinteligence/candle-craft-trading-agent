"""Versioned evidence contracts; acquisition time never substitutes for source time."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "cmc-evidence/1"


class EvidenceState(str, Enum):
    VERIFIED = "VERIFIED"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"
    MISSING = "MISSING"
    AMBIGUOUS = "AMBIGUOUS"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class Mode(str, Enum):
    OFF = "off"
    COLLECT = "collect"
    SHADOW = "shadow"
    DECISION = "decision"


class Comparison(str, Enum):
    CONSISTENT = "CONSISTENT"
    MATERIAL_DIVERGENCE = "MATERIAL_DIVERGENCE"
    TEMPORALLY_MISALIGNED = "TEMPORALLY_MISALIGNED"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    UNVERIFIED = "UNVERIFIED"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def utc(value: Any) -> datetime | None:
    if isinstance(value,bool):
        return None
    try:
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, (int, Decimal)) or (isinstance(value, str) and value.isdigit()):
            result = datetime.fromtimestamp(int(value), UTC)
        else:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def encoded(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def stable_id(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class Instrument(Frozen):
    cmc_id: int = Field(gt=0)
    market_id: int = Field(gt=0)
    exchange_id: int = Field(gt=0)
    exchange: str
    symbol: str
    base: str
    quote: str
    quote_id: int = Field(gt=0)
    contract_type: Literal["PERPETUAL"] = "PERPETUAL"
    margin_asset: str
    metadata_ref: str
    binding_ref: str
    metadata_acquired_at: datetime
    binding_approved_at: datetime
    map_acquired_at: datetime
    exchange_registry_acquired_at: datetime
    exchange_registry_ref: str
    binding_expires_at: datetime | None = None

    @property
    def key(self) -> str:
        return stable_id((self.cmc_id, self.market_id, self.exchange_id, self.exchange,
                          self.symbol, self.base, self.quote, self.quote_id, self.contract_type, self.margin_asset))


class Observation(Frozen):
    id: str
    provider: str = "CoinMarketCap"
    endpoint: str
    metric: str
    value: Decimal | None = None
    raw_value: str | None = None
    raw_type: str = "provider_numeric_text"
    units: str
    native_units: str
    source_at: datetime | None
    acquired_at: datetime
    state: EvidenceState
    reason: str = ""
    cmc_id: int | None = None
    exchange_id: int | None = None
    instrument: Instrument | None = None
    scope: str = "global"
    window_seconds: int | None = None
    funding_interval_seconds: int | None = None
    rate_semantics: str | None = None
    counting_convention: str | None = None
    provenance: str
    schema_version: str = SCHEMA_VERSION
    max_age_seconds: int = Field(default=300, gt=0)

    def status_at(self, as_of: datetime) -> EvidenceState:
        if self.schema_version != SCHEMA_VERSION:
            return EvidenceState.UNVERIFIED
        if self.acquired_at.tzinfo is None or as_of.tzinfo is None:
            return EvidenceState.UNVERIFIED
        if self.acquired_at > as_of:
            return EvidenceState.UNAVAILABLE
        if self.instrument is not None:
            expiry = self.instrument.binding_expires_at
            if expiry is not None and (expiry.tzinfo is None or as_of >= expiry):
                return EvidenceState.STALE
            times = (self.instrument.metadata_acquired_at, self.instrument.binding_approved_at, self.instrument.map_acquired_at,
                     self.instrument.exchange_registry_acquired_at)
            if any(t.tzinfo is None or t > as_of for t in times):
                return EvidenceState.UNAVAILABLE
            if (as_of - self.instrument.metadata_acquired_at).total_seconds() > 86400:
                return EvidenceState.STALE
        if self.source_at is None or self.source_at.tzinfo is None:
            return EvidenceState.UNVERIFIED if self.value is not None else self.state
        if self.source_at > self.acquired_at or self.source_at > as_of:
            return EvidenceState.UNVERIFIED
        if (as_of - self.source_at).total_seconds() > self.max_age_seconds:
            return EvidenceState.STALE
        return self.state

    @property
    def display(self) -> str:
        if self.value is None:
            return "N/A"
        return str(self.value) if self.state == EvidenceState.VERIFIED else "Unverified"


class Feature(Frozen):
    name: str
    version: str = "1"
    value: Decimal | None = None
    units: str
    state: EvidenceState
    evidence_ids: tuple[str, ...] = ()
    input_timestamps: tuple[datetime, ...] = ()
    acquired_timestamps: tuple[datetime, ...] = ()
    reason: str
    scope: str = "global"
    instrument_key: str | None = None
    cmc_id: int | None = None
    decision_relevance: str = "research context only"


class Snapshot(Frozen):
    observations: tuple[Observation, ...] = ()
    errors: tuple[str, ...] = ()
    batch_id: str = "N/A"
