from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator


MACRO_EVENT_SCHEMA_VERSION: Final = "cci_macro_events_v1"
MACRO_TAXONOMY_VERSION: Final = "cci_macro_taxonomy_v1"
MACRO_WINDOW_VERSION: Final = "cci_macro_windows_v1"
MACRO_USAGE: Final = "research_only"
US_EASTERN_TIMEZONE: Final = "America/New_York"


class MacroEventType(str, Enum):
    US_CPI = "US_CPI"
    US_PPI = "US_PPI"
    US_EMPLOYMENT_SITUATION = "US_EMPLOYMENT_SITUATION"
    US_JOLTS = "US_JOLTS"
    US_ECI = "US_ECI"
    US_PCE = "US_PCE"
    US_GDP = "US_GDP"
    FOMC_STATEMENT = "FOMC_STATEMENT"
    FOMC_PRESS_CONFERENCE = "FOMC_PRESS_CONFERENCE"
    FOMC_MINUTES = "FOMC_MINUTES"
    FED_SPEECH = "FED_SPEECH"
    OTHER_OFFICIAL_MACRO_EVENT = "OTHER_OFFICIAL_MACRO_EVENT"


class MacroEventCategory(str, Enum):
    INFLATION = "INFLATION"
    LABOR = "LABOR"
    GROWTH = "GROWTH"
    MONETARY_POLICY = "MONETARY_POLICY"
    FED_COMMUNICATION = "FED_COMMUNICATION"
    OTHER = "OTHER"


class ResearchPriority(str, Enum):
    TIER_1 = "TIER_1"
    TIER_2 = "TIER_2"
    NEUTRAL = "NEUTRAL"


class MacroVerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class MacroSourceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class MacroCalendarStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class MacroWindowPhase(str, Enum):
    PRE_EVENT = "PRE_EVENT"
    ACTIVE_EVENT = "ACTIVE_EVENT"
    POST_EVENT = "POST_EVENT"


class MacroEvent(BaseModel):
    event_id: str
    source: str
    source_event_id: str | None = None
    source_event_name: str
    normalized_event_type: MacroEventType
    scheduled_at_utc: datetime
    source_timezone: str
    country: str = "US"
    currency: str = "USD"
    event_category: MacroEventCategory
    speaker: str | None = None
    title: str | None = None
    institution: str
    research_priority: ResearchPriority
    verification_status: MacroVerificationStatus = MacroVerificationStatus.VERIFIED
    calendar_sequence: int | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator(
        "event_id",
        "source",
        "source_event_name",
        "source_timezone",
        "country",
        "currency",
        "institution",
    )
    @classmethod
    def _required_text(cls, value: str) -> str:
        normalized = " ".join(str(value).split())
        if not normalized:
            raise ValueError("macro event text fields must not be blank")
        return normalized

    @field_validator("source_event_id", "speaker")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(str(value).split())
        return normalized or None

    @field_validator("scheduled_at_utc", mode="before")
    @classmethod
    def _strict_aware_utc(cls, value: Any) -> datetime:
        return strict_aware_utc(value, field_name="scheduled_at_utc")


class MacroSourceHealth(BaseModel):
    source: str
    source_url: str
    status: MacroSourceStatus
    verification: MacroVerificationStatus
    fetched_at: datetime | None = None
    cache_hit: bool = False
    event_count: int = Field(default=0, ge=0)
    request_count: int = Field(default=0, ge=0)
    reason: str | None = None
    calendar_version: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("source", "source_url")
    @classmethod
    def _source_text(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("macro source fields must not be blank")
        return normalized

    @field_validator("fetched_at", mode="before")
    @classmethod
    def _fetched_at_aware(cls, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        return strict_aware_utc(value, field_name="fetched_at")


class MacroEventProximity(BaseModel):
    event_id: str
    normalized_event_type: MacroEventType
    scheduled_at_utc: datetime
    source: str
    research_priority: ResearchPriority
    verification_status: MacroVerificationStatus
    seconds_until_event: float | None = None
    minutes_until_event: float | None = None
    seconds_since_event: float | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("scheduled_at_utc", mode="before")
    @classmethod
    def _scheduled_at_aware(cls, value: Any) -> datetime:
        return strict_aware_utc(value, field_name="proximity_scheduled_at_utc")


class MacroEventWindow(BaseModel):
    pre_seconds: int = Field(ge=0)
    active_seconds: int = Field(ge=0)
    post_seconds: int = Field(ge=0)

    model_config = ConfigDict(frozen=True)


class MacroEventRiskSnapshot(BaseModel):
    schema_version: str = MACRO_EVENT_SCHEMA_VERSION
    taxonomy_version: str = MACRO_TAXONOMY_VERSION
    window_version: str = MACRO_WINDOW_VERSION
    usage: str = MACRO_USAGE
    generated_at: datetime
    calendar_status: MacroCalendarStatus
    sources: tuple[MacroSourceHealth, ...]
    events: tuple[MacroEvent, ...]
    nearest_upcoming_event: MacroEventProximity | None = None
    nearest_recent_event: MacroEventProximity | None = None
    events_next_24h: int = Field(default=0, ge=0)
    events_next_6h: int = Field(default=0, ge=0)
    events_next_1h: int = Field(default=0, ge=0)
    inside_event_window: bool = False
    window_event: MacroEventProximity | None = None
    window_phase: MacroWindowPhase | None = None
    provider_requests: int = Field(default=0, ge=0)
    cache_hit: bool = False

    model_config = ConfigDict(frozen=True)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _generated_at_aware(cls, value: Any) -> datetime:
        return strict_aware_utc(value, field_name="macro_generated_at")

    def symbol_context(self) -> MacroEventRiskContext | None:
        if self.calendar_status == MacroCalendarStatus.UNAVAILABLE:
            return None
        nearest = self.window_event or self.nearest_upcoming_event or self.nearest_recent_event
        degraded = tuple(
            source.source
            for source in self.sources
            if source.status != MacroSourceStatus.VERIFIED
        )
        return MacroEventRiskContext(
            calendar_status=self.calendar_status,
            status=(
                nearest.verification_status
                if nearest is not None
                else _calendar_verification(self.sources)
            ),
            nearest_event=(nearest.normalized_event_type if nearest is not None else None),
            scheduled_at_utc=(nearest.scheduled_at_utc if nearest is not None else None),
            seconds_until_event=(nearest.seconds_until_event if nearest is not None else None),
            minutes_until_event=(nearest.minutes_until_event if nearest is not None else None),
            seconds_since_event=(nearest.seconds_since_event if nearest is not None else None),
            research_priority=(nearest.research_priority if nearest is not None else None),
            source=(nearest.source if nearest is not None else None),
            inside_event_window=self.inside_event_window,
            window_phase=self.window_phase,
            degraded_sources=degraded,
        )


class MacroEventRiskContext(BaseModel):
    calendar_status: MacroCalendarStatus
    schema_version: str = MACRO_EVENT_SCHEMA_VERSION
    taxonomy_version: str = MACRO_TAXONOMY_VERSION
    window_version: str = MACRO_WINDOW_VERSION
    usage: str = MACRO_USAGE
    status: MacroVerificationStatus
    nearest_event: MacroEventType | None = None
    scheduled_at_utc: datetime | None = None
    seconds_until_event: float | None = None
    minutes_until_event: float | None = None
    seconds_since_event: float | None = None
    research_priority: ResearchPriority | None = None
    source: str | None = None
    inside_event_window: bool = False
    window_phase: MacroWindowPhase | None = None
    degraded_sources: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)

    @field_validator("scheduled_at_utc", mode="before")
    @classmethod
    def _context_scheduled_at_aware(cls, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        return strict_aware_utc(value, field_name="context_scheduled_at_utc")


EVENT_CATEGORY_BY_TYPE: Final[Mapping[MacroEventType, MacroEventCategory]] = {
    MacroEventType.US_CPI: MacroEventCategory.INFLATION,
    MacroEventType.US_PPI: MacroEventCategory.INFLATION,
    MacroEventType.US_EMPLOYMENT_SITUATION: MacroEventCategory.LABOR,
    MacroEventType.US_JOLTS: MacroEventCategory.LABOR,
    MacroEventType.US_ECI: MacroEventCategory.LABOR,
    MacroEventType.US_PCE: MacroEventCategory.INFLATION,
    MacroEventType.US_GDP: MacroEventCategory.GROWTH,
    MacroEventType.FOMC_STATEMENT: MacroEventCategory.MONETARY_POLICY,
    MacroEventType.FOMC_PRESS_CONFERENCE: MacroEventCategory.MONETARY_POLICY,
    MacroEventType.FOMC_MINUTES: MacroEventCategory.MONETARY_POLICY,
    MacroEventType.FED_SPEECH: MacroEventCategory.FED_COMMUNICATION,
    MacroEventType.OTHER_OFFICIAL_MACRO_EVENT: MacroEventCategory.OTHER,
}

RESEARCH_PRIORITY_BY_TYPE: Final[Mapping[MacroEventType, ResearchPriority]] = {
    MacroEventType.US_CPI: ResearchPriority.TIER_1,
    MacroEventType.US_PPI: ResearchPriority.TIER_1,
    MacroEventType.US_EMPLOYMENT_SITUATION: ResearchPriority.TIER_1,
    MacroEventType.US_JOLTS: ResearchPriority.TIER_2,
    MacroEventType.US_ECI: ResearchPriority.TIER_2,
    MacroEventType.US_PCE: ResearchPriority.TIER_1,
    MacroEventType.US_GDP: ResearchPriority.TIER_1,
    MacroEventType.FOMC_STATEMENT: ResearchPriority.TIER_1,
    MacroEventType.FOMC_PRESS_CONFERENCE: ResearchPriority.TIER_1,
    MacroEventType.FOMC_MINUTES: ResearchPriority.TIER_2,
    MacroEventType.FED_SPEECH: ResearchPriority.NEUTRAL,
    MacroEventType.OTHER_OFFICIAL_MACRO_EVENT: ResearchPriority.NEUTRAL,
}

RESEARCH_WINDOWS: Final[Mapping[ResearchPriority, MacroEventWindow]] = {
    ResearchPriority.TIER_1: MacroEventWindow(
        pre_seconds=60 * 60,
        active_seconds=60,
        post_seconds=60 * 60,
    ),
    ResearchPriority.TIER_2: MacroEventWindow(
        pre_seconds=30 * 60,
        active_seconds=60,
        post_seconds=30 * 60,
    ),
    ResearchPriority.NEUTRAL: MacroEventWindow(
        pre_seconds=0,
        active_seconds=0,
        post_seconds=0,
    ),
}

_BLS_PREFIX_TYPES: Final[tuple[tuple[str, MacroEventType], ...]] = (
    ("consumer price index", MacroEventType.US_CPI),
    ("producer price index", MacroEventType.US_PPI),
    ("the employment situation", MacroEventType.US_EMPLOYMENT_SITUATION),
    ("employment situation", MacroEventType.US_EMPLOYMENT_SITUATION),
    ("job openings and labor turnover survey", MacroEventType.US_JOLTS),
    ("employment cost index", MacroEventType.US_ECI),
)

_BEA_EXACT_TYPES: Final[Mapping[str, MacroEventType]] = {
    "gross domestic product": MacroEventType.US_GDP,
    "personal income and outlays": MacroEventType.US_PCE,
}

_FED_EXACT_TYPES: Final[Mapping[str, MacroEventType]] = {
    "fomc meeting": MacroEventType.FOMC_STATEMENT,
    "fomc press conference": MacroEventType.FOMC_PRESS_CONFERENCE,
    "fomc minutes": MacroEventType.FOMC_MINUTES,
}


def normalize_event_type(source: str, source_event_name: str) -> MacroEventType:
    source_key = str(source).strip().lower()
    name_key = " ".join(str(source_event_name).split()).casefold()
    if source_key == "bls":
        for prefix, event_type in _BLS_PREFIX_TYPES:
            if name_key == prefix or name_key.startswith(f"{prefix} ") or name_key.startswith(f"{prefix},"):
                return event_type
    elif source_key == "bea":
        return _BEA_EXACT_TYPES.get(name_key, MacroEventType.OTHER_OFFICIAL_MACRO_EVENT)
    elif source_key == "federal_reserve":
        exact = _FED_EXACT_TYPES.get(name_key)
        if exact is not None:
            return exact
        if name_key.startswith("speech - ") or name_key.startswith("discussion - "):
            return MacroEventType.FED_SPEECH
    return MacroEventType.OTHER_OFFICIAL_MACRO_EVENT


def build_event_id(
    *,
    source: str,
    source_event_id: str | None,
    normalized_event_type: MacroEventType,
    source_event_name: str,
    scheduled_at_utc: datetime,
) -> str:
    if source_event_id:
        identity = f"{source}|{source_event_id.strip()}"
    else:
        identity = "|".join(
            (
                source,
                normalized_event_type.value,
                " ".join(source_event_name.casefold().split()),
                strict_aware_utc(scheduled_at_utc).isoformat(),
            )
        )
    return sha256(identity.encode("utf-8")).hexdigest()[:24]


def build_macro_event_snapshot(
    *,
    generated_at: datetime,
    events: Sequence[MacroEvent],
    sources: Sequence[MacroSourceHealth],
    provider_requests: int = 0,
    cache_hit: bool = False,
) -> MacroEventRiskSnapshot:
    now = strict_aware_utc(generated_at, field_name="macro_generated_at")
    normalized_events = tuple(sorted(dedupe_macro_events(events), key=_event_sort_key))
    normalized_sources = tuple(sorted(sources, key=lambda item: item.source))
    upcoming = tuple(event for event in normalized_events if event.scheduled_at_utc >= now)
    recent = tuple(event for event in normalized_events if event.scheduled_at_utc < now)
    nearest_upcoming = _proximity(upcoming[0], now) if upcoming else None
    nearest_recent = _proximity(recent[-1], now) if recent else None
    window_event, window_phase = _active_window(normalized_events, now)
    return MacroEventRiskSnapshot(
        generated_at=now,
        calendar_status=calendar_status_from_sources(normalized_sources),
        sources=normalized_sources,
        events=normalized_events,
        nearest_upcoming_event=nearest_upcoming,
        nearest_recent_event=nearest_recent,
        events_next_24h=_upcoming_count(upcoming, now, hours=24),
        events_next_6h=_upcoming_count(upcoming, now, hours=6),
        events_next_1h=_upcoming_count(upcoming, now, hours=1),
        inside_event_window=window_event is not None,
        window_event=_proximity(window_event, now) if window_event is not None else None,
        window_phase=window_phase,
        provider_requests=provider_requests,
        cache_hit=cache_hit,
    )


def calendar_status_from_sources(
    sources: Sequence[MacroSourceHealth],
) -> MacroCalendarStatus:
    if not sources:
        return MacroCalendarStatus.UNAVAILABLE
    statuses = tuple(source.status for source in sources)
    if all(status == MacroSourceStatus.VERIFIED for status in statuses):
        return MacroCalendarStatus.VERIFIED
    if any(status in (MacroSourceStatus.VERIFIED, MacroSourceStatus.PARTIAL) for status in statuses):
        return MacroCalendarStatus.PARTIAL
    if any(status == MacroSourceStatus.STALE for status in statuses):
        return MacroCalendarStatus.STALE
    return MacroCalendarStatus.UNAVAILABLE


def dedupe_macro_events(events: Sequence[MacroEvent]) -> tuple[MacroEvent, ...]:
    selected: dict[tuple[str, datetime, str, str], MacroEvent] = {}
    for event in events:
        key = (
            event.normalized_event_type.value,
            event.scheduled_at_utc,
            event.institution.casefold(),
            (event.speaker or "").casefold(),
        )
        current = selected.get(key)
        if current is None or _dedupe_preference(event) < _dedupe_preference(current):
            selected[key] = event
    return tuple(selected.values())


def strict_aware_utc(value: Any, *, field_name: str = "timestamp") -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} is not a valid aware timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _proximity(event: MacroEvent, now: datetime) -> MacroEventProximity:
    seconds = round((event.scheduled_at_utc - now).total_seconds(), 3)
    if seconds >= 0:
        return MacroEventProximity(
            event_id=event.event_id,
            normalized_event_type=event.normalized_event_type,
            scheduled_at_utc=event.scheduled_at_utc,
            source=event.source,
            research_priority=event.research_priority,
            seconds_until_event=seconds,
            verification_status=event.verification_status,
            minutes_until_event=round(seconds / 60, 3),
        )
    return MacroEventProximity(
        event_id=event.event_id,
        normalized_event_type=event.normalized_event_type,
        scheduled_at_utc=event.scheduled_at_utc,
        source=event.source,
        research_priority=event.research_priority,
        seconds_since_event=round(-seconds, 3),
        verification_status=event.verification_status,
    )


def _calendar_verification(
    sources: Sequence[MacroSourceHealth],
) -> MacroVerificationStatus:
    if any(source.verification == MacroVerificationStatus.VERIFIED for source in sources):
        return MacroVerificationStatus.VERIFIED
    return MacroVerificationStatus.UNVERIFIED


def _active_window(
    events: Sequence[MacroEvent], now: datetime
) -> tuple[MacroEvent | None, MacroWindowPhase | None]:
    candidates: list[tuple[int, float, str, MacroEvent, MacroWindowPhase]] = []
    priority_order = {
        ResearchPriority.TIER_1: 0,
        ResearchPriority.TIER_2: 1,
        ResearchPriority.NEUTRAL: 2,
    }
    for event in events:
        window = RESEARCH_WINDOWS[event.research_priority]
        if window.pre_seconds == window.active_seconds == window.post_seconds == 0:
            continue
        seconds_until = (event.scheduled_at_utc - now).total_seconds()
        if 0 < seconds_until <= window.pre_seconds:
            phase = MacroWindowPhase.PRE_EVENT
        else:
            seconds_since = -seconds_until
            if 0 <= seconds_since <= window.active_seconds:
                phase = MacroWindowPhase.ACTIVE_EVENT
            elif window.active_seconds < seconds_since <= window.post_seconds:
                phase = MacroWindowPhase.POST_EVENT
            else:
                continue
        candidates.append(
            (
                priority_order[event.research_priority],
                abs(seconds_until),
                event.normalized_event_type.value,
                event,
                phase,
            )
        )
    if not candidates:
        return None, None
    selected = min(candidates, key=lambda item: item[:3])
    return selected[3], selected[4]


def _upcoming_count(events: Sequence[MacroEvent], now: datetime, *, hours: int) -> int:
    maximum = hours * 60 * 60
    return sum(
        1
        for event in events
        if 0 <= (event.scheduled_at_utc - now).total_seconds() <= maximum
    )


def _event_sort_key(event: MacroEvent) -> tuple[Any, ...]:
    return (
        event.scheduled_at_utc,
        event.normalized_event_type.value,
        event.institution.casefold(),
        (event.speaker or "").casefold(),
        event.event_id,
    )


def _dedupe_preference(event: MacroEvent) -> tuple[Any, ...]:
    source_order = {"federal_reserve": 0, "bls": 1, "bea": 2}
    return (
        source_order.get(event.source, 99),
        -(event.calendar_sequence or 0),
        event.event_id,
    )


__all__ = [
    "EVENT_CATEGORY_BY_TYPE",
    "MACRO_EVENT_SCHEMA_VERSION",
    "MACRO_TAXONOMY_VERSION",
    "MACRO_USAGE",
    "MACRO_WINDOW_VERSION",
    "MacroCalendarStatus",
    "MacroEvent",
    "MacroEventCategory",
    "MacroEventProximity",
    "MacroEventRiskContext",
    "MacroEventRiskSnapshot",
    "MacroEventType",
    "MacroEventWindow",
    "MacroSourceHealth",
    "MacroSourceStatus",
    "MacroVerificationStatus",
    "MacroWindowPhase",
    "RESEARCH_PRIORITY_BY_TYPE",
    "RESEARCH_WINDOWS",
    "ResearchPriority",
    "US_EASTERN_TIMEZONE",
    "build_event_id",
    "build_macro_event_snapshot",
    "calendar_status_from_sources",
    "dedupe_macro_events",
    "normalize_event_type",
    "strict_aware_utc",
]
