from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.x_hype_prompt_agent.hype_scorer import score_item
from src.x_hype_prompt_agent.models import AgentConfig, NewsItem
from src.x_hype_prompt_agent.normalizer import normalize_news_item
from src.x_hype_prompt_agent.prompt_builder import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    build_chatgpt_prompt,
    build_telegram_message,
)


NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


def _scored():
    item = normalize_news_item(
        NewsItem(
            title="Bitcoin ETF inflows surge as BlackRock demand returns",
            source_name="CoinDesk",
            source_tier=1,
            url="https://example.com/btc-etf",
            summary="Institutional flow returns to BTC.",
            published_at=NOW - timedelta(minutes=42),
            fetched_at=NOW,
        )
    )
    return score_item(item, config=AgentConfig(), now=NOW)


def test_prompt_formatting_contains_required_sections() -> None:
    scored = _scored()

    prompt = build_chatgpt_prompt(scored)

    assert "Act as the Candle Craft Intelligence X content writer." in prompt
    assert "Headline:" in prompt
    assert "Source:" in prompt
    assert "Why it matters:" in prompt
    assert "Market angle:" in prompt
    assert "Visual angle:" in prompt
    assert "Return exactly:" in prompt
    assert "1. Short X post" in prompt
    assert "2. More aggressive engagement version" in prompt
    assert "3. Image-generation prompt" in prompt
    assert "4. Alt text" in prompt
    assert "5. Optional thread angle" in prompt
    assert "Do not invent facts beyond the headline and source." in prompt
    assert "auto-post" not in prompt.lower()


def test_prompt_contains_240_character_post_rules() -> None:
    scored = _scored()

    prompt = build_chatgpt_prompt(scored)

    assert "Under 800 chars" not in prompt
    assert "maximum 240 characters total" in prompt
    assert "including hashtags and brand ending" in prompt
    assert "The 240-character limit also includes body text and line breaks." in prompt
    assert "Character count: <number>" in prompt
    assert "Use exactly 3 relevant hashtags by default." in prompt
    assert "Use 4 hashtags only if the post stays under 240 characters." in prompt
    assert "Do not use more than 4 hashtags." in prompt
    assert "Candle Craft Intelligence / Signal. Structure. Execution." not in prompt
    assert "Candle Craft Intelligence" in prompt
    assert "CCI | Signal. Structure. Execution." in prompt
    assert "The wolf tracks liquidity." in prompt


def test_prompt_contains_hashtag_image_and_alt_text_instructions() -> None:
    scored = _scored()

    prompt = build_chatgpt_prompt(scored)

    assert "#BitcoinETF #BTC #CryptoMarkets" in prompt
    assert "If the story is not about Bitcoin, do not force Bitcoin hashtags." in prompt
    assert "Image-generation prompt rules:" in prompt
    assert "Return one professional image-generation prompt" in prompt
    assert "Prefer 16:9 landscape." in prompt
    assert "Candle Craft premium dark/orange/gold style" in prompt
    assert "Avoid fake charts with specific made-up price levels." in prompt
    assert "Alt text rules:" in prompt
    assert "1 to 2 short sentences" in prompt
    assert "Include no financial advice." in prompt
    assert "Do not invent facts beyond the headline and source." in prompt


def test_telegram_message_formatting_is_copy_ready() -> None:
    scored = _scored()
    prompt = build_chatgpt_prompt(scored)

    message = build_telegram_message(scored, prompt, now=NOW)
    visible_header = message.split("Copy into ChatGPT:", maxsplit=1)[0]

    assert "🟠 CCI X PROMPT" in message
    assert "Copy into ChatGPT:" in message
    assert f"Score: {scored.final_score}/100 | ETF_FLOW | CoinDesk" in message
    assert "CANDLE CRAFT X HYPE IDEA" not in message
    assert "COPY THIS PROMPT INTO CHATGPT:" not in message
    assert "Why this could perform:" not in message
    assert "Market angle:" not in visible_header
    assert "Suggested visual angle:" not in message
    assert "42 minutes old" not in message
    assert "Institutional liquidity flow graphic" in message
    assert prompt in message
    assert len(message) <= TELEGRAM_MAX_MESSAGE_LENGTH