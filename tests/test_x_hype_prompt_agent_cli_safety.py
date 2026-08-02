from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.x_hype_prompt_agent import runner as runner_module
from src.x_hype_prompt_agent import telegram_sender as telegram_sender_module
from src.x_hype_prompt_agent.config import ConfigError, telegram_chat_id, telegram_token
from src.x_hype_prompt_agent.hype_scorer import score_item
from src.x_hype_prompt_agent.models import AgentConfig, NewsItem, NewsSourceConfig
from src.x_hype_prompt_agent.normalizer import normalize_news_item
from src.x_hype_prompt_agent.news_sources import fetch_rss_source
from src.x_hype_prompt_agent.runner import build_arg_parser, main, run_once
from src.x_hype_prompt_agent.storage import XHypeStorage
from src.x_hype_prompt_agent.telegram_sender import TelegramXHypeSender


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _sources_file(tmp_path) -> str:
    path = tmp_path / "sources.yaml"
    path.write_text(
        """sources:
  - name: Mock RSS
    type: rss
    url: https://rss.test/feed
    tier: 1
    enabled: true
    categories: [markets]
    reliability_weight: 1.0
""",
        encoding="utf-8",
    )
    return str(path)


def _rss_response() -> str:
    return """
    <rss><channel><item>
      <title>Bitcoin ETF inflows hit $1.2 billion as BlackRock demand surges</title>
      <link>https://example.test/etf-flow</link>
      <description>Institutional ETF flow returns while BTC liquidity improves.</description>
      <pubDate>Sun, 02 Aug 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>
    """


def _stored_item() -> NewsItem:
    return normalize_news_item(
        NewsItem(
            title="Bitcoin ETF inflows surge as BlackRock demand returns",
            source_name="Mock RSS",
            source_tier=1,
            url="https://example.test/etf-flow",
            summary="Institutional ETF flow returns.",
            published_at=NOW - timedelta(minutes=30),
            fetched_at=NOW,
        )
    )


def test_cli_parser_defaults_to_safe_mode() -> None:
    args = build_arg_parser().parse_args([])

    assert args.live_send is False
    assert args.dry_run is False


def test_cli_rejects_conflicting_send_modes() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--dry-run", "--live-send"])

    assert exc_info.value.code == 2


def test_cli_live_send_is_forwarded_only_when_explicit(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(runner_module, "load_dotenv_if_available", lambda: None)
    monkeypatch.setenv("TELEGRAM_X_HYPE_BOT_TOKEN", "dedicated-token")
    monkeypatch.setenv("TELEGRAM_X_HYPE_CHAT_ID", "dedicated-chat")
    monkeypatch.setattr(runner_module, "run_once", lambda **kwargs: captured.update(kwargs))

    assert main(["--live-send"]) == 0
    assert captured["live_send"] is True
    assert captured["dry_run"] is False


def test_default_run_uses_mocked_rss_and_never_constructs_telegram_sender(tmp_path, monkeypatch, capsys) -> None:
    rss_calls = 0

    def rss_handler(request: httpx.Request) -> httpx.Response:
        nonlocal rss_calls
        rss_calls += 1
        assert str(request.url) == "https://rss.test/feed"
        return httpx.Response(200, text=_rss_response())

    rss_client = httpx.Client(transport=httpx.MockTransport(rss_handler))

    def fail_if_telegram_sender_is_created(*args, **kwargs):
        raise AssertionError("default invocation must not construct the Telegram sender")

    monkeypatch.setattr(runner_module, "TelegramXHypeSender", fail_if_telegram_sender_is_created)

    try:
        summary = run_once(
            database_path=str(tmp_path / "x_hype.sqlite"),
            sources_config=_sources_file(tmp_path),
            min_score=0,
            max_prompts_per_run=1,
            http_client=rss_client,
        )
    finally:
        rss_client.close()

    assert rss_calls == 1
    assert summary.items_fetched == 1
    assert summary.prompts_sent == 0
    assert "CCI X PROMPT" in capsys.readouterr().out


def test_sender_default_is_dry_run_and_never_calls_transport(capsys) -> None:
    calls = 0

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("safe sender default must not call Telegram")

    client = httpx.Client(transport=httpx.MockTransport(fail_if_called), base_url="https://telegram.test")
    try:
        result = TelegramXHypeSender(bot_token="dedicated-token", chat_id="dedicated-chat", client=client).send_message(
            "preview"
        )
    finally:
        client.close()

    assert result.status == "dry_run"
    assert calls == 0
    assert "preview" in capsys.readouterr().out


def test_explicit_live_sender_uses_only_dedicated_credentials(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    monkeypatch.setenv("TELEGRAM_X_HYPE_BOT_TOKEN", "dedicated-token")
    monkeypatch.setenv("TELEGRAM_X_HYPE_CHAT_ID", "dedicated-chat")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "legacy-signal-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "legacy-signal-chat")
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
    try:
        result = TelegramXHypeSender(client=client, max_retries=0).send_message("live prompt", dry_run=False)
    finally:
        client.close()

    assert result.sent
    assert len(requests) == 1
    assert requests[0].url.path == "/botdedicated-token/sendMessage"
    assert json.loads(requests[0].content)["chat_id"] == "dedicated-chat"
    assert "legacy-signal-token" not in str(requests[0].url)


def test_legacy_signal_credentials_never_enable_live_send(monkeypatch) -> None:
    calls = 0

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("legacy signal credentials must not reach Telegram")

    monkeypatch.delenv("TELEGRAM_X_HYPE_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_X_HYPE_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "legacy-signal-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "legacy-signal-chat")
    client = httpx.Client(transport=httpx.MockTransport(fail_if_called), base_url="https://telegram.test")
    try:
        result = TelegramXHypeSender(client=client, max_retries=0).send_message("live prompt", dry_run=False)
    finally:
        client.close()

    assert telegram_token() is None
    assert telegram_chat_id() is None
    assert result.error == "missing_telegram_credentials"
    assert calls == 0


def test_missing_live_credentials_fail_before_rss_or_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "load_dotenv_if_available", lambda: None)
    monkeypatch.delenv("TELEGRAM_X_HYPE_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_X_HYPE_CHAT_ID", raising=False)
    monkeypatch.setattr(
        runner_module,
        "fetch_news_from_sources",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("RSS must not be fetched")),
    )
    database_path = tmp_path / "x_hype.sqlite"

    with pytest.raises(ConfigError, match="TELEGRAM_X_HYPE_BOT_TOKEN"):
        run_once(live_send=True, database_path=str(database_path), sources_config=_sources_file(tmp_path))

    assert not database_path.exists()


def test_rss_rate_limit_is_handled_without_live_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="rate limited")

    source = NewsSourceConfig(name="Mock RSS", type="rss", url="https://rss.test/feed", tier=1)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert fetch_rss_source(source, client=client) == ()
    finally:
        client.close()

    assert calls == 1


def test_live_runtime_database_path_is_rejected(tmp_path) -> None:
    runtime_path = tmp_path / "main_live_runtime.sqlite"

    with pytest.raises(ConfigError, match="live Runtime database"):
        XHypeStorage(runtime_path)

    assert not runtime_path.exists()


def test_database_deduplication_survives_storage_restart(tmp_path) -> None:
    database_path = tmp_path / "x_hype.sqlite"
    item = _stored_item()
    first_storage = XHypeStorage(database_path)
    scored = score_item(item, config=AgentConfig(), now=NOW)
    news_item_id = first_storage.store_news_item(item)
    scored_item_id = first_storage.store_scored_item(news_item_id, scored)
    first_storage.store_sent_prompt(
        news_item_id=news_item_id,
        scored_item_id=scored_item_id,
        telegram_message_id=1,
        prompt_text="prompt",
        telegram_text="telegram",
        final_score=scored.final_score,
        sent_at=NOW,
    )

    restarted_storage = XHypeStorage(database_path)

    assert restarted_storage.recently_sent_duplicate(item, duplicate_window_days=7, now=NOW)
    assert restarted_storage.count_sent_today(now=NOW) == 1
