from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import AgentConfig, NewsItem, ScoredItem, utc_now
from .narrative_classifier import (
    AI_CRYPTO,
    BTC_LIQUIDITY,
    CONTROVERSY,
    DEFI_RISK,
    DOLLAR_DXY,
    ETF_FLOWS,
    ETH_ECOSYSTEM,
    EXCHANGE_BINANCE_COINBASE_MEXC,
    FUNDING_OPEN_INTEREST,
    GENERIC_ALTCOIN_NOISE,
    HACK_EXPLOIT_SECURITY,
    LAYER2,
    LIQUIDATION_SQUEEZE,
    LISTING_DELISTING,
    MACRO_FED_CPI_RATES,
    MARKET_STRUCTURE,
    MEMECOIN_ROTATION,
    REGULATION_SEC_CFTC_EU_MICA,
    RUMOR,
    RWA,
    SOLANA_ECOSYSTEM,
    SPONSORED_OR_LOW_QUALITY,
    STABLECOIN_LIQUIDITY,
    WHALE_MOVEMENT,
    classify_narratives,
)
from .normalizer import title_similarity


@dataclass(frozen=True)
class NarrativeCluster:
    narrative: str
    item_count: int
    source_count: int
    best_source_tier: int


@dataclass(frozen=True)
class ScoringContext:
    clusters: dict[str, NarrativeCluster]
    recent_sent_titles: tuple[str, ...] = ()
    recent_sent_urls: tuple[str, ...] = ()


NARRATIVE_WEIGHTS = {
    BTC_LIQUIDITY: 88,
    ETH_ECOSYSTEM: 75,
    SOLANA_ECOSYSTEM: 78,
    ETF_FLOWS: 94,
    MACRO_FED_CPI_RATES: 83,
    DOLLAR_DXY: 76,
    REGULATION_SEC_CFTC_EU_MICA: 86,
    EXCHANGE_BINANCE_COINBASE_MEXC: 80,
    LISTING_DELISTING: 76,
    HACK_EXPLOIT_SECURITY: 93,
    WHALE_MOVEMENT: 83,
    LIQUIDATION_SQUEEZE: 89,
    FUNDING_OPEN_INTEREST: 78,
    STABLECOIN_LIQUIDITY: 81,
    AI_CRYPTO: 66,
    RWA: 66,
    MEMECOIN_ROTATION: 67,
    DEFI_RISK: 79,
    LAYER2: 64,
    MARKET_STRUCTURE: 74,
    CONTROVERSY: 72,
    RUMOR: 45,
    SPONSORED_OR_LOW_QUALITY: 8,
    GENERIC_ALTCOIN_NOISE: 16,
}

ENGAGEMENT_TRIGGERS = {
    "controversy": 18,
    "fear": 12,
    "greed": 10,
    "missed": 12,
    "liquidity": 14,
    "liquidation": 20,
    "cascade": 20,
    "whale": 18,
    "blackrock": 18,
    "institutional": 15,
    "approval": 15,
    "rejection": 15,
    "lawsuit": 17,
    "investigation": 17,
    "hack": 22,
    "exploit": 22,
    "insolvency": 20,
    "freeze": 16,
    "delisting": 16,
    "listing": 11,
    "short squeeze": 20,
    "long squeeze": 18,
    "record": 12,
    "biggest": 14,
    "massive": 12,
}

HYPE_TRIGGERS = {
    "surge": 12,
    "soar": 12,
    "plunge": 14,
    "crash": 16,
    "sudden": 14,
    "breakout": 12,
    "breakdown": 12,
    "panic": 15,
    "euphoria": 12,
    "mania": 15,
    "shock": 16,
    "record": 12,
    "billion": 10,
    "million": 7,
}

LOW_QUALITY_PHRASES = (
    "best coins to buy",
    "top 10",
    "top tokens",
    "price prediction",
    "could reach",
    "next 100x",
    "presale",
    "sponsored",
    "press release",
    "partner content",
)


def build_scoring_context(
    items: tuple[NewsItem, ...] | list[NewsItem],
    *,
    now: datetime | None = None,
    lookback_hours: int = 24,
    recent_sent_titles: tuple[str, ...] = (),
    recent_sent_urls: tuple[str, ...] = (),
) -> ScoringContext:
    reference = _as_utc(now or utc_now())
    grouped: dict[str, dict[str, object]] = {}
    for item in items:
        age = item.age_hours(reference)
        if age is not None and age > lookback_hours:
            continue
        for narrative in classify_narratives(item):
            bucket = grouped.setdefault(
                narrative,
                {"items": 0, "sources": set(), "best_tier": 3},
            )
            bucket["items"] = int(bucket["items"]) + 1
            sources = bucket["sources"]
            if isinstance(sources, set):
                sources.add(item.source_name)
            bucket["best_tier"] = min(int(bucket["best_tier"]), item.source_tier)

    clusters = {
        narrative: NarrativeCluster(
            narrative=narrative,
            item_count=int(data["items"]),
            source_count=len(data["sources"]) if isinstance(data["sources"], set) else 0,
            best_source_tier=int(data["best_tier"]),
        )
        for narrative, data in grouped.items()
    }
    return ScoringContext(
        clusters=clusters,
        recent_sent_titles=recent_sent_titles,
        recent_sent_urls=recent_sent_urls,
    )


def score_item(
    item: NewsItem,
    *,
    config: AgentConfig | None = None,
    context: ScoringContext | None = None,
    now: datetime | None = None,
) -> ScoredItem:
    cfg = config or AgentConfig()
    reference = _as_utc(now or utc_now())
    narratives = classify_narratives(item)
    text = _story_text(item)

    engagement_score = _engagement_score(text, narratives)
    hype_score = _hype_score(text, narratives)
    market_impact_score = _market_impact_score(text, narratives)
    freshness_score = _freshness_score(item, reference, cfg.freshness_half_life_hours)
    source_quality_score = _source_quality_score(item)
    controversy_score = _controversy_score(text, narratives)
    cluster_bonus = cluster_bonus_for_narratives(narratives, context)
    narrative_score = _clamp(max((NARRATIVE_WEIGHTS.get(narrative, 30) for narrative in narratives), default=25) + cluster_bonus)
    duplicate_penalty = _duplicate_penalty(item, context)
    risk_penalty = _risk_penalty(item, text, narratives, reference, cfg, cluster_bonus)

    weighted = (
        engagement_score * 0.20
        + hype_score * 0.15
        + market_impact_score * 0.22
        + narrative_score * 0.14
        + controversy_score * 0.10
        + freshness_score * 0.10
        + source_quality_score * 0.09
    )
    final_score = _clamp(round(weighted - duplicate_penalty - risk_penalty))
    category = _category_for(narratives, final_score, text, cfg.breaking_news_score)
    explanation = _explanation(
        narratives=narratives,
        cluster_bonus=cluster_bonus,
        duplicate_penalty=duplicate_penalty,
        risk_penalty=risk_penalty,
        freshness_score=freshness_score,
        source_quality_score=source_quality_score,
    )

    return ScoredItem(
        news_item=item,
        engagement_score=engagement_score,
        hype_score=hype_score,
        market_impact_score=market_impact_score,
        narrative_score=narrative_score,
        controversy_score=controversy_score,
        freshness_score=freshness_score,
        source_quality_score=source_quality_score,
        duplicate_penalty=duplicate_penalty,
        risk_penalty=risk_penalty,
        final_score=final_score,
        category=category,
        narratives=narratives,
        explanation=explanation,
    )


def cluster_bonus_for_narratives(narratives: tuple[str, ...], context: ScoringContext | None) -> int:
    if context is None:
        return 0
    best_bonus = 0
    for narrative in narratives:
        cluster = context.clusters.get(narrative)
        if cluster is None:
            continue
        if cluster.source_count >= 2 and cluster.best_source_tier <= 2:
            best_bonus = max(best_bonus, 8 if cluster.item_count == 2 else 12)
    return best_bonus


def _engagement_score(text: str, narratives: tuple[str, ...]) -> int:
    score = 24
    if {BTC_LIQUIDITY, ETF_FLOWS, LIQUIDATION_SQUEEZE, WHALE_MOVEMENT, HACK_EXPLOIT_SECURITY} & set(narratives):
        score += 22
    if {REGULATION_SEC_CFTC_EU_MICA, EXCHANGE_BINANCE_COINBASE_MEXC, MACRO_FED_CPI_RATES} & set(narratives):
        score += 16
    if SOLANA_ECOSYSTEM in narratives or ETH_ECOSYSTEM in narratives:
        score += 9
    score += _trigger_points(text, ENGAGEMENT_TRIGGERS, cap=38)
    if _has_large_number(text):
        score += 12
    return _clamp(score)


def _hype_score(text: str, narratives: tuple[str, ...]) -> int:
    score = 20 + _trigger_points(text, HYPE_TRIGGERS, cap=45)
    if {LIQUIDATION_SQUEEZE, HACK_EXPLOIT_SECURITY, MEMECOIN_ROTATION, ETF_FLOWS} & set(narratives):
        score += 24
    if {BTC_LIQUIDITY, SOLANA_ECOSYSTEM, STABLECOIN_LIQUIDITY} & set(narratives):
        score += 12
    if SPONSORED_OR_LOW_QUALITY in narratives:
        score -= 25
    return _clamp(score)


def _market_impact_score(text: str, narratives: tuple[str, ...]) -> int:
    score = max((NARRATIVE_WEIGHTS.get(narrative, 25) for narrative in narratives), default=25)
    if _contains_any(text, ("btc", "bitcoin", "eth", "ethereum", "solana", "sol ")):
        score += 8
    if _contains_any(text, ("liquidity", "flow", "inflow", "outflow", "funding", "open interest")):
        score += 10
    if _has_large_number(text):
        score += 8
    if GENERIC_ALTCOIN_NOISE in narratives:
        score -= 35
    if SPONSORED_OR_LOW_QUALITY in narratives:
        score -= 45
    return _clamp(score)


def _freshness_score(item: NewsItem, now: datetime, half_life_hours: int) -> int:
    age = item.age_hours(now)
    if age is None:
        return 46
    if age <= 0.25:
        return 100
    if age <= 1:
        return 98
    if age >= 72:
        return 0
    half_life = max(1, half_life_hours)
    score = 35 + 65 * math.pow(0.5, age / half_life)
    if age > 24:
        score -= 20
    return _clamp(round(score))


def _source_quality_score(item: NewsItem) -> int:
    base = {1: 92, 2: 74, 3: 43}.get(min(3, max(1, item.source_tier)), 60)
    reliability = min(1.25, max(0.5, item.source_reliability_weight))
    adjusted = base * (0.82 + reliability * 0.18)
    return _clamp(round(adjusted))


def _controversy_score(text: str, narratives: tuple[str, ...]) -> int:
    score = 12
    if CONTROVERSY in narratives:
        score += 45
    if REGULATION_SEC_CFTC_EU_MICA in narratives:
        score += 25
    if HACK_EXPLOIT_SECURITY in narratives:
        score += 24
    if RUMOR in narratives:
        score += 18
    if _contains_any(text, ("backlash", "accused", "denies", "lawsuit", "probe", "investigation", "ban")):
        score += 18
    return _clamp(score)


def _duplicate_penalty(item: NewsItem, context: ScoringContext | None) -> int:
    if context is None:
        return 0
    if item.canonical_url and item.canonical_url in context.recent_sent_urls:
        return 70
    for sent_title in context.recent_sent_titles:
        if title_similarity(item.normalized_title, sent_title) >= 0.9:
            return 55
    return 0


def _risk_penalty(
    item: NewsItem,
    text: str,
    narratives: tuple[str, ...],
    now: datetime,
    config: AgentConfig,
    cluster_bonus: int,
) -> int:
    penalty = 0
    if SPONSORED_OR_LOW_QUALITY in narratives or _contains_any(text, LOW_QUALITY_PHRASES):
        penalty += 38
    if GENERIC_ALTCOIN_NOISE in narratives:
        penalty += 24
    if RUMOR in narratives and item.source_tier >= 3:
        penalty += 24
    if not item.url.strip() and config.require_source_url:
        penalty += 20
    age = item.age_hours(now)
    if age is not None:
        if age > 72:
            penalty += 80
        elif age > 24 and HACK_EXPLOIT_SECURITY not in narratives and REGULATION_SEC_CFTC_EU_MICA not in narratives:
            penalty += 20
    if item.source_tier >= 3 and cluster_bonus == 0 and not config.allow_tier_3_only_items:
        penalty += 14
    return _clamp(penalty)


def _category_for(narratives: tuple[str, ...], final_score: int, text: str, breaking_news_score: int) -> str:
    if final_score >= breaking_news_score and _contains_any(text, ("breaking", "just in", "sudden", "emergency")):
        return "BREAKING"
    if HACK_EXPLOIT_SECURITY in narratives:
        return "SECURITY"
    if ETF_FLOWS in narratives:
        return "ETF_FLOW"
    if BTC_LIQUIDITY in narratives:
        return "BTC_LIQUIDITY"
    if MACRO_FED_CPI_RATES in narratives or DOLLAR_DXY in narratives:
        return "MACRO"
    if REGULATION_SEC_CFTC_EU_MICA in narratives:
        return "REGULATION"
    if EXCHANGE_BINANCE_COINBASE_MEXC in narratives or LISTING_DELISTING in narratives:
        return "EXCHANGE"
    if WHALE_MOVEMENT in narratives:
        return "WHALE"
    if LIQUIDATION_SQUEEZE in narratives:
        return "LIQUIDATION"
    if SOLANA_ECOSYSTEM in narratives:
        return "SOLANA"
    if ETH_ECOSYSTEM in narratives:
        return "ETH"
    if {AI_CRYPTO, RWA, MEMECOIN_ROTATION, LAYER2, DEFI_RISK, STABLECOIN_LIQUIDITY} & set(narratives):
        return "NARRATIVE_ROTATION"
    if GENERIC_ALTCOIN_NOISE in narratives or SPONSORED_OR_LOW_QUALITY in narratives:
        return "REJECT_GENERIC"
    return "ALTCOIN_HIGH_SIGNAL" if final_score >= 70 else "REJECT_GENERIC"


def _explanation(
    *,
    narratives: tuple[str, ...],
    cluster_bonus: int,
    duplicate_penalty: int,
    risk_penalty: int,
    freshness_score: int,
    source_quality_score: int,
) -> str:
    positives: list[str] = []
    if narratives:
        positives.append(f"narratives: {', '.join(narratives[:4])}")
    if cluster_bonus:
        positives.append(f"cluster bonus +{cluster_bonus}")
    positives.append(f"freshness {freshness_score}/100")
    positives.append(f"source quality {source_quality_score}/100")
    if duplicate_penalty:
        positives.append(f"duplicate penalty -{duplicate_penalty}")
    if risk_penalty:
        positives.append(f"risk penalty -{risk_penalty}")
    return "; ".join(positives)


def _story_text(item: NewsItem) -> str:
    return f" {item.title} {item.summary} {item.raw_category} ".lower()


def _trigger_points(text: str, triggers: dict[str, int], *, cap: int) -> int:
    points = 0
    for phrase, value in triggers.items():
        if phrase in text:
            points += value
    return min(cap, points)


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _has_large_number(text: str) -> bool:
    return re.search(r"(\$?\d+(?:\.\d+)?\s?(billion|million|bn|m)\b|\$\d)", text) is not None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clamp(value: int | float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(round(value))))
