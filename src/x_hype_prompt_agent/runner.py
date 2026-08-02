from __future__ import annotations

import argparse
import logging
import time
from collections import Counter
from pathlib import Path

import httpx

from .config import (
    DEFAULT_AGENT_CONFIG_PATH,
    DEFAULT_SOURCES_CONFIG_PATH,
    configured_database_path,
    configured_log_level,
    load_agent_config,
    load_dotenv_if_available,
    load_source_configs,
)
from .config import ConfigError, telegram_chat_id as configured_telegram_chat_id, telegram_token
from .hype_scorer import build_scoring_context, score_item
from .logging_utils import configure_logging
from .models import AgentRunSummary, ScoredItem, utc_now
from .news_sources import fetch_news_from_sources
from .normalizer import deduplicate_items
from .prompt_builder import build_chatgpt_prompt, build_telegram_message
from .safety import evaluate_safety
from .storage import XHypeStorage
from .telegram_sender import TelegramXHypeSender

logger = logging.getLogger(__name__)


def run_once(
    *,
    dry_run: bool = False,
    live_send: bool = False,
    min_score: int | None = None,
    max_prompts_per_run: int | None = None,
    max_prompts_per_day: int | None = None,
    database_path: str | None = None,
    sources_config: str | None = None,
    agent_config: str | None = None,
    print_top: int = 0,
    include_rejected: bool = False,
    log_level: str | None = None,
    http_client: httpx.Client | None = None,
) -> AgentRunSummary:
    if dry_run and live_send:
        raise ConfigError("--dry-run and --live-send cannot be used together.")

    dry_run = not live_send

    load_dotenv_if_available()
    if live_send and (not telegram_token() or not configured_telegram_chat_id()):
        raise ConfigError(
            "Live Telegram sending requires TELEGRAM_X_HYPE_BOT_TOKEN and "
            "TELEGRAM_X_HYPE_CHAT_ID."
        )

    configure_logging(configured_log_level(log_level))
    now = utc_now()

    cfg = load_agent_config(Path(agent_config) if agent_config else DEFAULT_AGENT_CONFIG_PATH).with_overrides(
        min_score_to_send=min_score,
        max_prompts_per_run=max_prompts_per_run,
        max_prompts_per_day=max_prompts_per_day,
    )
    source_path = Path(sources_config) if sources_config else DEFAULT_SOURCES_CONFIG_PATH
    sources = load_source_configs(source_path)
    enabled_sources = tuple(source for source in sources if source.enabled)
    storage = XHypeStorage(configured_database_path(database_path))
    run_id = storage.start_agent_run(mode="dry_run" if dry_run else "send", started_at=now)

    errors: list[str] = []
    prompts_sent = 0
    prompts_selected = 0
    rejected_count = 0
    rejection_reasons: Counter[str] = Counter()

    try:
        logger.info("X hype prompt agent run started", extra={"mode": "dry_run" if dry_run else "send"})
        logger.info("Sources loaded", extra={"sources": len(sources), "enabled_sources": len(enabled_sources)})

        fetched_items = fetch_news_from_sources(enabled_sources, client=http_client)
        logger.info("Total items fetched", extra={"count": len(fetched_items)})
        deduped_items = deduplicate_items(fetched_items)
        logger.info(
            "Items deduplicated",
            extra={"before": len(fetched_items), "after": len(deduped_items), "removed": len(fetched_items) - len(deduped_items)},
        )

        recent_titles, recent_urls = storage.recent_sent_fingerprints(
            duplicate_window_days=cfg.duplicate_window_days,
            now=now,
        )
        context = build_scoring_context(
            deduped_items,
            now=now,
            lookback_hours=cfg.lookback_hours,
            recent_sent_titles=recent_titles,
            recent_sent_urls=recent_urls,
        )
        scored_records: list[tuple[ScoredItem, int, int]] = []
        for item in deduped_items:
            scored = score_item(item, config=cfg, context=context, now=now)
            news_item_id = storage.store_news_item(scored.news_item)
            scored_item_id = storage.store_scored_item(news_item_id, scored)
            scored_records.append((scored, news_item_id, scored_item_id))

        scored_records.sort(key=lambda record: record[0].final_score, reverse=True)
        logger.info("Items scored", extra={"count": len(scored_records)})
        if scored_records:
            logger.info(
                "Top scores",
                extra={
                    "top_scores": [
                        {
                            "score": scored.final_score,
                            "category": scored.category,
                            "source": scored.news_item.source_name,
                            "title": scored.news_item.title[:120],
                        }
                        for scored, _, _ in scored_records[:5]
                    ]
                },
            )
        if print_top:
            _print_top(scored_records, print_top)

        sender = TelegramXHypeSender(
            disable_web_page_preview=cfg.telegram_disable_web_page_preview,
        )
        sent_today = storage.count_sent_today(now=now)
        for scored, news_item_id, scored_item_id in scored_records:
            if prompts_selected >= cfg.max_prompts_per_run:
                break
            if sent_today + prompts_sent >= cfg.max_prompts_per_day:
                reason = "daily_prompt_limit_reached"
                _store_rejection(storage, scored, news_item_id, reason)
                rejection_reasons[reason] += 1
                rejected_count += 1
                break

            prompt_text = build_chatgpt_prompt(scored)
            telegram_text = build_telegram_message(scored, prompt_text, now=now)
            duplicate_recent = storage.recently_sent_duplicate(
                scored.news_item,
                duplicate_window_days=cfg.duplicate_window_days,
                now=now,
            )
            decision = evaluate_safety(
                scored,
                config=cfg,
                prompt_text=prompt_text,
                telegram_text=telegram_text,
                duplicate_recent=duplicate_recent,
                now=now,
            )
            if not decision.allowed:
                reason = ",".join(decision.reasons)
                _store_rejection(storage, scored, news_item_id, reason)
                rejection_reasons.update(decision.reasons)
                rejected_count += 1
                if dry_run and include_rejected:
                    print(f"REJECTED {scored.final_score}/100 {scored.news_item.source_name}: {scored.news_item.title}")
                    print(f"Reasons: {reason}")
                continue

            result = sender.send_message(telegram_text, dry_run=dry_run)
            prompts_selected += 1
            if result.sent:
                storage.store_sent_prompt(
                    news_item_id=news_item_id,
                    scored_item_id=scored_item_id,
                    telegram_message_id=result.telegram_message_id,
                    prompt_text=prompt_text,
                    telegram_text=telegram_text,
                    final_score=scored.final_score,
                    sent_at=utc_now(),
                )
                prompts_sent += 1
            elif dry_run and result.status == "dry_run":
                logger.info("Dry-run prompt printed", extra={"score": scored.final_score, "title": scored.news_item.title})
            else:
                error = result.error or result.detail
                errors.append(error)
                _store_rejection(storage, scored, news_item_id, f"telegram_send_failed:{error}")
                rejected_count += 1

        logger.info(
            "Rejected count",
            extra={"count": rejected_count, "reasons": dict(rejection_reasons)},
        )
        logger.info(
            "Run finished",
            extra={
                "items_fetched": len(fetched_items),
                "items_scored": len(scored_records),
                "prompts_sent": prompts_sent,
                "dry_run_selected": prompts_selected if dry_run else 0,
                "items_rejected": rejected_count,
                "errors": len(errors),
            },
        )
        return AgentRunSummary(
            items_fetched=len(fetched_items),
            items_scored=len(scored_records),
            prompts_sent=prompts_sent,
            items_rejected=rejected_count,
            errors=tuple(errors),
        )
    except Exception as exc:
        logger.exception("X hype prompt agent run failed")
        errors.append(f"{exc.__class__.__name__}: {exc}")
        raise
    finally:
        storage.finish_agent_run(
            run_id,
            items_fetched=locals().get("fetched_items") and len(fetched_items) or 0,
            items_scored=locals().get("scored_records") and len(scored_records) or 0,
            prompts_sent=prompts_sent,
            items_rejected=rejected_count,
            errors=tuple(errors),
            finished_at=utc_now(),
        )


def run_watch(
    *,
    watch_interval_sec: int | None = None,
    **kwargs: object,
) -> None:
    while True:
        try:
            summary = run_once(**kwargs)
            logger.info("Watch iteration complete", extra={"summary": summary})
        except KeyboardInterrupt:
            raise
        except ConfigError:
            raise
        except Exception as exc:
            logger.exception("Watch iteration failed", extra={"error": exc.__class__.__name__})
        interval = int(watch_interval_sec or kwargs.get("watch_interval_sec") or 3600)
        time.sleep(max(1, interval))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the standalone Candle Craft X hype prompt agent.")
    parser.add_argument("--dry-run", action="store_true", help="Compatibility option; safe preview is the default.")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
    parser.add_argument(
        "--live-send",
        action="store_true",
        help="Explicitly opt in to sending through the dedicated X Hype Telegram bot.",
    )
    parser.add_argument("--watch", action="store_true", help="Run continuously.")
    parser.add_argument("--watch-interval-sec", type=int, default=None)
    parser.add_argument("--min-score", type=int, default=None)
    parser.add_argument("--max-prompts-per-run", type=int, default=None)
    parser.add_argument("--max-prompts-per-day", type=int, default=None)
    parser.add_argument("--database-path", default=None)
    parser.add_argument("--sources-config", default=None)
    parser.add_argument("--agent-config", default=None)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--print-top", type=int, default=0)
    parser.add_argument("--include-rejected", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.dry_run and args.live_send:
        parser.error("--dry-run and --live-send cannot be used together.")
    if args.live_send:
        load_dotenv_if_available()
        if not telegram_token() or not configured_telegram_chat_id():
            parser.error(
                "--live-send requires TELEGRAM_X_HYPE_BOT_TOKEN and TELEGRAM_X_HYPE_CHAT_ID."
            )
    kwargs = {
        "dry_run": args.dry_run,
        "min_score": args.min_score,
        "max_prompts_per_run": args.max_prompts_per_run,
        "max_prompts_per_day": args.max_prompts_per_day,
        "live_send": args.live_send,
        "database_path": args.database_path,
        "sources_config": args.sources_config,
        "agent_config": args.agent_config,
        "print_top": args.print_top,
        "include_rejected": args.include_rejected,
        "log_level": args.log_level,
    }
    if args.watch:
        run_watch(watch_interval_sec=args.watch_interval_sec, **kwargs)
    else:
        run_once(**kwargs)
    return 0


def _store_rejection(storage: XHypeStorage, scored: ScoredItem, news_item_id: int, reason: str) -> None:
    storage.store_rejected_item(
        news_item_id=news_item_id,
        reason=reason,
        score_snapshot=scored.score_snapshot(),
    )


def _print_top(scored_records: list[tuple[ScoredItem, int, int]], limit: int) -> None:
    print("Top X hype scored stories:")
    for scored, _, _ in scored_records[: max(0, limit)]:
        print(f"{scored.final_score:>3}/100 {scored.category:<18} {scored.news_item.source_name}: {scored.news_item.title}")
