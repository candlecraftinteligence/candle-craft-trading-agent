from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.x_hype_prompt_agent.hype_scorer import build_scoring_context, cluster_bonus_for_narratives, score_item
from src.x_hype_prompt_agent.models import AgentConfig, NewsItem
from src.x_hype_prompt_agent.narrative_classifier import ETF_FLOWS
from src.x_hype_prompt_agent.normalizer import normalize_news_item


NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


def _item(
    title: str,
    *,
    source: str = "CoinDesk",
    tier: int = 2,
    hours_old: float = 1,
    summary: str = "",
    url: str = "https://example.com/story",
) -> NewsItem:
    return normalize_news_item(
        NewsItem(
            title=title,
            source_name=source,
            source_tier=tier,
            url=url,
            summary=summary,
            published_at=NOW - timedelta(hours=hours_old),
            fetched_at=NOW,
        )
    )


def test_btc_etf_story_scores_high() -> None:
    item = _item(
        "Bitcoin ETF inflows hit $1.2 billion as BlackRock demand surges",
        summary="Institutional ETF flow returns while BTC liquidity improves.",
    )

    scored = score_item(item, config=AgentConfig(), now=NOW)

    assert scored.final_score >= 80
    assert scored.category == "ETF_FLOW"
    assert ETF_FLOWS in scored.narratives


def test_generic_altcoin_story_scores_low() -> None:
    item = _item(
        "Tiny altcoin announces community partnership and roadmap update",
        source="High Noise Feed",
        tier=3,
        summary="Sponsored price prediction coverage says it could reach a new high.",
    )

    scored = score_item(item, config=AgentConfig(), now=NOW)

    assert scored.final_score < 50
    assert scored.category == "REJECT_GENERIC"


def test_hack_security_story_scores_high() -> None:
    item = _item(
        "DeFi protocol suffers $80 million exploit as attacker drains liquidity pool",
        summary="Security breach raises contagion concerns across DeFi markets.",
    )

    scored = score_item(item, config=AgentConfig(), now=NOW)

    assert scored.final_score >= 80
    assert scored.category == "SECURITY"


def test_cluster_bonus_logic_rewards_multi_source_narrative() -> None:
    first = _item(
        "Bitcoin ETF inflows surge as BlackRock demand returns",
        source="CoinDesk",
        tier=1,
        url="https://example.com/a",
    )
    second = _item(
        "BTC ETF flow accelerates after large institutional inflow",
        source="The Block",
        tier=2,
        url="https://example.com/b",
    )

    context = build_scoring_context((first, second), now=NOW, lookback_hours=24)

    assert cluster_bonus_for_narratives((ETF_FLOWS,), context) >= 8
    assert score_item(first, config=AgentConfig(), context=context, now=NOW).narrative_score > score_item(
        first,
        config=AgentConfig(),
        now=NOW,
    ).narrative_score
