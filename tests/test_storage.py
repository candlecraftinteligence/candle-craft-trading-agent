from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.x_hype_prompt_agent.hype_scorer import score_item
from src.x_hype_prompt_agent.models import AgentConfig, NewsItem
from src.x_hype_prompt_agent.normalizer import normalize_news_item
from src.x_hype_prompt_agent.storage import XHypeStorage, open_initialized_database


NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


def _item(title: str, *, url: str = "https://example.com/story") -> NewsItem:
    return normalize_news_item(
        NewsItem(
            title=title,
            source_name="CoinDesk",
            source_tier=1,
            url=url,
            summary="Institutional flow returns.",
            published_at=NOW - timedelta(minutes=30),
            fetched_at=NOW,
        )
    )


def _stored_score(storage: XHypeStorage, item: NewsItem):
    scored = score_item(item, config=AgentConfig(), now=NOW)
    news_item_id = storage.store_news_item(item)
    scored_item_id = storage.store_scored_item(news_item_id, scored)
    return scored, news_item_id, scored_item_id


def test_database_insert_read(tmp_path) -> None:
    db_path = tmp_path / "x_hype.sqlite"
    storage = XHypeStorage(db_path)
    item = _item("Bitcoin ETF inflows surge as BlackRock demand returns")

    scored, news_item_id, scored_item_id = _stored_score(storage, item)
    sent_id = storage.store_sent_prompt(
        news_item_id=news_item_id,
        scored_item_id=scored_item_id,
        telegram_message_id=123,
        prompt_text="copy-ready prompt",
        telegram_text="telegram text",
        final_score=scored.final_score,
        sent_at=NOW,
    )

    row = storage.get_news_item_row(news_item_id)

    assert sent_id == 1
    assert row is not None
    assert row["title"] == item.title
    assert row["canonical_url"] == "https://example.com/story"


def test_database_schema_tables_exist(tmp_path) -> None:
    db_path = tmp_path / "x_hype.sqlite"

    with open_initialized_database(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }

    assert {
        "news_items",
        "scored_items",
        "sent_prompts",
        "rejected_items",
        "agent_runs",
    } <= tables


def test_duplicate_prompt_detection(tmp_path) -> None:
    storage = XHypeStorage(tmp_path / "x_hype.sqlite")
    original = _item("Bitcoin ETF inflows surge as BlackRock demand returns", url="https://example.com/a")
    scored, news_item_id, scored_item_id = _stored_score(storage, original)
    storage.store_sent_prompt(
        news_item_id=news_item_id,
        scored_item_id=scored_item_id,
        telegram_message_id=1,
        prompt_text="prompt",
        telegram_text="telegram",
        final_score=scored.final_score,
        sent_at=NOW,
    )
    repeat = _item("BTC ETF inflows surge as BlackRock demand returns", url="https://example.com/b")

    assert storage.recently_sent_duplicate(repeat, duplicate_window_days=7, now=NOW)


def test_daily_max_prompt_limit_count(tmp_path) -> None:
    storage = XHypeStorage(tmp_path / "x_hype.sqlite")
    for index in range(6):
        item = _item(f"Bitcoin ETF inflows surge {index}", url=f"https://example.com/{index}")
        scored, news_item_id, scored_item_id = _stored_score(storage, item)
        storage.store_sent_prompt(
            news_item_id=news_item_id,
            scored_item_id=scored_item_id,
            telegram_message_id=index,
            prompt_text="prompt",
            telegram_text="telegram",
            final_score=scored.final_score,
            sent_at=NOW - timedelta(hours=index),
        )

    assert storage.count_sent_today(now=NOW) == 6


def test_agent_run_summary_persists(tmp_path) -> None:
    storage = XHypeStorage(tmp_path / "x_hype.sqlite")
    run_id = storage.start_agent_run(mode="dry_run", started_at=NOW)

    storage.finish_agent_run(
        run_id,
        items_fetched=10,
        items_scored=8,
        prompts_sent=0,
        items_rejected=6,
        errors=("feed_error",),
        finished_at=NOW,
    )

    row = storage.latest_agent_run()

    assert row is not None
    assert row["mode"] == "dry_run"
    assert row["items_fetched"] == 10
    assert row["items_rejected"] == 6
    assert "feed_error" in row["errors_json"]
