from typing import Any

from pydantic import BaseModel, ConfigDict


class CciEventDTO(BaseModel):
    """One CCI lifecycle event. Fields come from the source; none are inferred."""

    model_config = ConfigDict(extra="forbid")

    cci_event_id: str
    cci_setup_id: str
    event_type: str
    state: str
    occurred_at: str
    outcome_code: str | None = None
    payload: dict[str, Any] | None = None


class CciSetupDTO(BaseModel):
    """Public setup projection. `lifecycle` is the fixture event list unchanged."""

    model_config = ConfigDict(extra="forbid")

    cci_setup_id: str
    symbol: str
    timeframe: str
    direction: str
    quality_tier: str
    title: str
    thesis_summary: str
    evidence: list[dict[str, Any]]
    lifecycle: list[dict[str, Any]]
    lifecycle_state: str
    outcome_code: str | None
    opened_at: str
    resolved_at: str | None
    resolved: bool
    synthetic: bool
    disclaimer: str
