from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class NewsSourceConfig:
    name: str
    type: str
    url: str
    tier: int = 2
    enabled: bool = True
    categories: tuple[str, ...] = ()
    reliability_weight: float = 1.0
    notes: str = ""

    @property
    def normalized_tier(self) -> int:
        return min(3, max(1, int(self.tier)))


@dataclass(frozen=True)
class AgentConfig:
    min_score_to_send: int = 80
    breaking_news_score: int = 90
    max_prompts_per_run: int = 2
    max_prompts_per_day: int = 6
    lookback_hours: int = 24
    freshness_half_life_hours: int = 8
    duplicate_window_days: int = 7
    watch_interval_sec: int = 3600
    allow_tier_3_only_items: bool = False
    require_source_url: bool = True
    telegram_disable_web_page_preview: bool = False

    def with_overrides(self, **overrides: Any) -> AgentConfig:
        values = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **values)


@dataclass(frozen=True)
class NewsItem:
    title: str
    source_name: str
    url: str
    published_at: datetime | None
    summary: str = ""
    fetched_at: datetime = field(default_factory=utc_now)
    raw_category: str = ""
    source_tier: int = 2
    canonical_url: str = ""
    normalized_title: str = ""
    normalized_id: str = ""
    content_hash: str = ""
    source_reliability_weight: float = 1.0

    def age_hours(self, now: datetime | None = None) -> float | None:
        if self.published_at is None:
            return None
        reference = now or utc_now()
        reference = reference if reference.tzinfo else reference.replace(tzinfo=UTC)
        published = self.published_at if self.published_at.tzinfo else self.published_at.replace(tzinfo=UTC)
        seconds = (reference.astimezone(UTC) - published.astimezone(UTC)).total_seconds()
        return max(0.0, seconds / 3600)


@dataclass(frozen=True)
class ScoredItem:
    news_item: NewsItem
    engagement_score: int
    hype_score: int
    market_impact_score: int
    narrative_score: int
    controversy_score: int
    freshness_score: int
    source_quality_score: int
    duplicate_penalty: int
    risk_penalty: int
    final_score: int
    category: str
    narratives: tuple[str, ...]
    explanation: str

    def score_snapshot(self) -> dict[str, Any]:
        return {
            "engagement_score": self.engagement_score,
            "hype_score": self.hype_score,
            "market_impact_score": self.market_impact_score,
            "narrative_score": self.narrative_score,
            "controversy_score": self.controversy_score,
            "freshness_score": self.freshness_score,
            "source_quality_score": self.source_quality_score,
            "duplicate_penalty": self.duplicate_penalty,
            "risk_penalty": self.risk_penalty,
            "final_score": self.final_score,
            "category": self.category,
            "narratives": list(self.narratives),
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TelegramSendResult:
    status: str
    detail: str
    telegram_message_id: int | None = None
    error: str | None = None

    @property
    def sent(self) -> bool:
        return self.status == "sent"


@dataclass(frozen=True)
class AgentRunSummary:
    items_fetched: int
    items_scored: int
    prompts_sent: int
    items_rejected: int
    errors: tuple[str, ...] = ()
