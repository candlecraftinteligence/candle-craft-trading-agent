from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data.candle_integrity import normalize_utc_timestamp


class ContextStatus(str, Enum):
    VERIFIED = "VERIFIED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class ContextValue(BaseModel):
    value: Any | None = None
    source: str
    observed_at: datetime | None = None
    age_seconds: float | None = None
    status: ContextStatus
    reason: str | None = None
    cache_hit: bool = False

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    @field_validator("source")
    @classmethod
    def _source_not_blank(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("context source must not be blank")
        return normalized

    @field_validator("observed_at", mode="before")
    @classmethod
    def _normalize_observed_at(cls, value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        return normalize_utc_timestamp(value, field_name="observed_at")

    @field_validator("age_seconds")
    @classmethod
    def _age_non_negative(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return round(max(float(value), 0.0), 3)

    @classmethod
    def unavailable(cls, *, source: str, reason: str) -> ContextValue:
        return cls(
            value=None,
            source=source,
            status=ContextStatus.UNAVAILABLE,
            reason=reason,
        )

    @classmethod
    def error(cls, *, source: str, reason: str) -> ContextValue:
        return cls(
            value=None,
            source=source,
            status=ContextStatus.ERROR,
            reason=reason,
        )

    @property
    def usable_for_research(self) -> bool:
        return self.value is not None and self.status in (
            ContextStatus.VERIFIED,
            ContextStatus.STALE,
        )

    def research_payload(self) -> dict[str, Any] | None:
        if not self.usable_for_research:
            return None
        return {
            "usage": "research_only",
            **self.model_dump(mode="json"),
        }


class BtcContextPayload(BaseModel):
    symbol: str = "BTCUSDT"
    bias_12h: ContextValue
    structure_2h: ContextValue
    execution_15m: ContextValue
    atr_15m: ContextValue
    atr_pct_15m: ContextValue
    funding_rate: ContextValue
    open_interest: ContextValue
    open_interest_change_pct: ContextValue

    model_config = ConfigDict(frozen=True)


class BtcDominancePayload(BaseModel):
    btc_dominance_pct: Decimal

    model_config = ConfigDict(frozen=True)


class WeekendContextPayload(BaseModel):
    is_weekend: bool
    utc_weekday: int = Field(ge=0, le=6)
    utc_weekday_name: str
    session_label: str

    model_config = ConfigDict(frozen=True)


class GlobalContextDiagnostics(BaseModel):
    global_context_status: ContextStatus
    btc_context_status: ContextStatus
    btc_d_context_status: ContextStatus
    weekend_context_status: ContextStatus
    btc_d_cache_hit: bool = False

    model_config = ConfigDict(frozen=True)


class GlobalContextSnapshot(BaseModel):
    generated_at: datetime
    btc_context: ContextValue
    btc_d_context: ContextValue
    weekend_context: ContextValue
    diagnostics: GlobalContextDiagnostics

    model_config = ConfigDict(frozen=True)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _normalize_generated_at(cls, value: Any) -> datetime:
        return normalize_utc_timestamp(value, field_name="generated_at")

    def strategy_context(self) -> dict[str, Any | None]:
        weekend_payload = self.weekend_context.research_payload()
        return {
            "btc_context": self.btc_context.research_payload(),
            "btc_d_context": self.btc_d_context.research_payload(),
            "weekend_filter": weekend_payload,
        }


def build_global_context_snapshot(
    *,
    generated_at: datetime,
    btc_context: ContextValue,
    btc_d_context: ContextValue,
    weekend_context: ContextValue,
) -> GlobalContextSnapshot:
    statuses = (
        btc_context.status,
        btc_d_context.status,
        weekend_context.status,
    )
    if all(status == ContextStatus.VERIFIED for status in statuses):
        global_status = ContextStatus.VERIFIED
    elif ContextStatus.ERROR in statuses:
        global_status = ContextStatus.ERROR
    elif ContextStatus.UNAVAILABLE in statuses:
        global_status = ContextStatus.UNAVAILABLE
    else:
        global_status = ContextStatus.STALE
    return GlobalContextSnapshot(
        generated_at=generated_at,
        btc_context=btc_context,
        btc_d_context=btc_d_context,
        weekend_context=weekend_context,
        diagnostics=GlobalContextDiagnostics(
            global_context_status=global_status,
            btc_context_status=btc_context.status,
            btc_d_context_status=btc_d_context.status,
            weekend_context_status=weekend_context.status,
            btc_d_cache_hit=btc_d_context.cache_hit,
        ),
    )
