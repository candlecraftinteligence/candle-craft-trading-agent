from __future__ import annotations

from datetime import UTC, datetime

import httpx

from src.x_hype_prompt_agent.models import NewsItem, NewsSourceConfig
from src.x_hype_prompt_agent.narrative_classifier import ETF_FLOWS, classify_narratives
from src.x_hype_prompt_agent.news_sources import parse_rss_feed
from src.x_hype_prompt_agent.normalizer import deduplicate_items, normalize_title, normalize_url
from src.x_hype_prompt_agent.telegram_sender import TelegramXHypeSender


NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


def _source() -> NewsSourceConfig:
    return NewsSourceConfig(
        name="Unit Feed",
        type="rss",
        url="https://example.com/rss",
        tier=2,
        categories=("markets",),
        reliability_weight=1.0,
    )


def _item(title: str, url: str) -> NewsItem:
    return NewsItem(
        title=title,
        source_name="Unit Feed",
        source_tier=2,
        url=url,
        published_at=NOW,
        fetched_at=NOW,
    )


def test_rss_parsing_with_sample_feed_data() -> None:
    feed = """
    <rss><channel>
      <item>
        <title>Bitcoin ETF inflows surge as BlackRock demand returns</title>
        <link>https://example.com/story?utm_source=newsletter</link>
        <description><![CDATA[Institutional flow hits the market.]]></description>
        <pubDate>Thu, 02 Jul 2026 11:30:00 GMT</pubDate>
        <category>ETF</category>
      </item>
    </channel></rss>
    """

    items = parse_rss_feed(feed, source=_source(), fetched_at=NOW)

    assert len(items) == 1
    assert items[0].title == "Bitcoin ETF inflows surge as BlackRock demand returns"
    assert items[0].url == "https://example.com/story?utm_source=newsletter"
    assert items[0].summary == "Institutional flow hits the market."
    assert items[0].published_at == datetime(2026, 7, 2, 11, 30, tzinfo=UTC)


def test_malformed_feed_handling_returns_empty_tuple() -> None:
    assert parse_rss_feed("<rss><channel><item>", source=_source(), fetched_at=NOW) == ()


def test_url_normalization_removes_tracking_params() -> None:
    assert (
        normalize_url("HTTPS://www.Example.com/path/?utm_source=x&fbclid=1&id=42#frag")
        == "https://example.com/path?id=42"
    )


def test_title_normalization_collapses_assets_noise_and_punctuation() -> None:
    assert normalize_title("Latest Bitcoin News: THE ETF Flow Update!!!") == "btc etf flow"


def test_deduplication_by_url_and_title() -> None:
    items = (
        _item("Bitcoin ETF inflows surge", "https://example.com/story?utm_source=x"),
        _item("Bitcoin ETF inflows surge", "https://example.com/story"),
        _item("BTC ETF inflows surge", "https://example.com/other"),
    )

    deduped = deduplicate_items(items)

    assert len(deduped) == 1


def test_narrative_classification_detects_btc_etf_story() -> None:
    item = _item("Bitcoin ETF inflows surge as BlackRock demand returns", "https://example.com/story")

    narratives = classify_narratives(item)

    assert ETF_FLOWS in narratives


def test_dry_run_mode_does_not_send(capsys) -> None:
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry-run must not call Telegram")

    client = httpx.Client(transport=httpx.MockTransport(fail_if_called), base_url="https://telegram.test")
    sender = TelegramXHypeSender(bot_token=None, chat_id=None, client=client)

    result = sender.send_message("formatted telegram message", dry_run=True)

    assert result.status == "dry_run"
    assert "formatted telegram message" in capsys.readouterr().out
