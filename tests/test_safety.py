from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from src.x_hype_prompt_agent.hype_scorer import score_item
from src.x_hype_prompt_agent.models import AgentConfig, NewsItem
from src.x_hype_prompt_agent.normalizer import normalize_news_item
from src.x_hype_prompt_agent.prompt_builder import build_chatgpt_prompt, build_telegram_message
from src.x_hype_prompt_agent.safety import evaluate_safety


NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


def _scored(title: str, *, hours_old: float = 1, summary: str = "", url: str = "https://example.com/story"):
    item = normalize_news_item(
        NewsItem(
            title=title,
            source_name="Unit Feed",
            source_tier=2,
            url=url,
            summary=summary,
            published_at=NOW - timedelta(hours=hours_old),
            fetched_at=NOW,
        )
    )
    return score_item(item, config=AgentConfig(), now=NOW)


def _decision(scored, *, duplicate_recent: bool = False, config: AgentConfig | None = None):
    prompt = build_chatgpt_prompt(scored)
    message = build_telegram_message(scored, prompt, now=NOW)
    return evaluate_safety(
        scored,
        config=config or AgentConfig(),
        prompt_text=prompt,
        telegram_text=message,
        duplicate_recent=duplicate_recent,
        now=NOW,
    )


def test_old_story_rejection() -> None:
    scored = _scored("Bitcoin ETF inflows surge as BlackRock demand returns", hours_old=80)

    decision = _decision(scored)

    assert "story_older_than_72_hours" in decision.reasons


def test_sponsored_content_rejection() -> None:
    scored = _scored("Sponsored: Best coins to buy before the next 100x presale", summary="Partner content.")

    decision = _decision(scored)

    assert "sponsored_or_low_quality_content" in decision.reasons


def test_low_score_rejection() -> None:
    scored = _scored("Small altcoin publishes community roadmap update", summary="Minor ecosystem note.")

    decision = _decision(scored, config=AgentConfig(min_score_to_send=80))

    assert any(reason.startswith("score_below_minimum") for reason in decision.reasons)


def test_duplicate_prompt_rejection() -> None:
    scored = _scored("Bitcoin ETF inflows surge as BlackRock demand returns")

    decision = _decision(scored, duplicate_recent=True)

    assert "duplicate_prompt_recently_sent" in decision.reasons


def test_telegram_message_too_long_rejection() -> None:
    scored = _scored("Bitcoin ETF inflows surge as BlackRock demand returns")
    prompt = build_chatgpt_prompt(scored)
    long_message = "x" * 5000

    decision = evaluate_safety(
        scored,
        config=AgentConfig(),
        prompt_text=prompt,
        telegram_text=long_message,
        now=NOW,
    )

    assert "telegram_message_too_long" in decision.reasons


def test_missing_url_rejection() -> None:
    scored = _scored("Bitcoin ETF inflows surge as BlackRock demand returns", url="")
    scored = replace(scored, final_score=90)

    decision = _decision(scored)

    assert "missing_source_url" in decision.reasons
