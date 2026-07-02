from __future__ import annotations

import re
from datetime import datetime

from .models import AgentConfig, SafetyDecision, ScoredItem, utc_now
from .narrative_classifier import RUMOR, SPONSORED_OR_LOW_QUALITY
from .prompt_builder import TELEGRAM_MAX_MESSAGE_LENGTH


def evaluate_safety(
    scored: ScoredItem,
    *,
    config: AgentConfig,
    prompt_text: str,
    telegram_text: str,
    duplicate_recent: bool = False,
    now: datetime | None = None,
) -> SafetyDecision:
    item = scored.news_item
    reference = now or utc_now()
    reasons: list[str] = []
    warnings: list[str] = []

    if config.require_source_url and not item.url.strip():
        reasons.append("missing_source_url")
    if duplicate_recent:
        reasons.append("duplicate_prompt_recently_sent")
    if scored.final_score < config.min_score_to_send:
        reasons.append(f"score_below_minimum:{scored.final_score}<{config.min_score_to_send}")

    age = item.age_hours(reference)
    if age is None:
        warnings.append("published_at_missing_freshness_unverified")
    elif age > 72:
        reasons.append("story_older_than_72_hours")
    elif age > 24 and scored.final_score < config.breaking_news_score:
        reasons.append("old_low_impact_story")

    if SPONSORED_OR_LOW_QUALITY in scored.narratives or _looks_sponsored(scored):
        reasons.append("sponsored_or_low_quality_content")
    if RUMOR in scored.narratives and scored.final_score < config.breaking_news_score:
        reasons.append("unsupported_rumor_below_breaking_threshold")
    elif RUMOR in scored.narratives:
        warnings.append("rumor_must_be_labeled_unconfirmed")

    if not prompt_text.strip():
        reasons.append("empty_prompt_text")
    if not telegram_text.strip():
        reasons.append("empty_telegram_text")
    if len(telegram_text) > TELEGRAM_MAX_MESSAGE_LENGTH:
        reasons.append("telegram_message_too_long")
    if _has_bad_control_chars(telegram_text):
        reasons.append("malformed_telegram_payload_control_chars")

    return SafetyDecision(allowed=not reasons, reasons=tuple(reasons), warnings=tuple(warnings))


def _looks_sponsored(scored: ScoredItem) -> bool:
    text = f"{scored.news_item.title} {scored.news_item.summary}".lower()
    return any(
        phrase in text
        for phrase in (
            "sponsored",
            "press release",
            "partner content",
            "advertisement",
            "best coins to buy",
            "top 10",
            "price prediction",
            "next 100x",
            "presale",
        )
    )


def _has_bad_control_chars(text: str) -> bool:
    return re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text) is not None
