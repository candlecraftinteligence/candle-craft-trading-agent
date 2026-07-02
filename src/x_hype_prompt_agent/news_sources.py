from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Iterable

import httpx

from .models import NewsItem, NewsSourceConfig, utc_now

logger = logging.getLogger(__name__)

DEFAULT_FETCH_TIMEOUT = 15.0


def fetch_news_from_sources(
    sources: Iterable[NewsSourceConfig],
    *,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> tuple[NewsItem, ...]:
    close_client = client is None
    http_client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    items: list[NewsItem] = []
    try:
        for source in sources:
            if not source.enabled:
                continue
            if source.type.lower() != "rss":
                logger.info("Skipping unsupported source type", extra={"source": source.name, "type": source.type})
                continue
            source_items = fetch_rss_source(source, client=http_client)
            logger.info(
                "Fetched source items",
                extra={"source": source.name, "count": len(source_items), "tier": source.normalized_tier},
            )
            items.extend(source_items)
    finally:
        if close_client:
            http_client.close()
    return tuple(items)


def fetch_rss_source(source: NewsSourceConfig, *, client: httpx.Client) -> tuple[NewsItem, ...]:
    fetched_at = utc_now()
    try:
        response = client.get(source.url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("RSS source fetch failed", extra={"source": source.name, "error": str(exc)})
        return ()
    except Exception as exc:
        logger.warning("RSS source fetch failed", extra={"source": source.name, "error": exc.__class__.__name__})
        return ()
    return parse_rss_feed(response.text, source=source, fetched_at=fetched_at)


def parse_rss_feed(feed_text: str, *, source: NewsSourceConfig, fetched_at: datetime | None = None) -> tuple[NewsItem, ...]:
    if not feed_text or not feed_text.strip():
        return ()
    fetched = fetched_at or utc_now()
    try:
        root = ET.fromstring(feed_text)
    except ET.ParseError as exc:
        logger.warning("Malformed RSS feed", extra={"source": source.name, "error": str(exc)})
        return ()

    entries = _rss_items(root)
    if not entries:
        entries = _atom_entries(root)

    items: list[NewsItem] = []
    for entry in entries:
        try:
            item = _parse_entry(entry, source=source, fetched_at=fetched)
        except Exception as exc:
            logger.warning("Malformed RSS entry skipped", extra={"source": source.name, "error": exc.__class__.__name__})
            continue
        if item is not None:
            items.append(item)
    return tuple(items)


def _parse_entry(entry: ET.Element, *, source: NewsSourceConfig, fetched_at: datetime) -> NewsItem | None:
    title = _clean_text(_first_text(entry, "title"))
    url = _clean_text(_first_text(entry, "link")) or _atom_link(entry) or _clean_text(_first_text(entry, "guid"))
    if not title:
        return None

    summary = _clean_text(
        _first_text(entry, "description")
        or _first_text(entry, "summary")
        or _first_text(entry, "content")
        or _first_text(entry, "content:encoded")
    )
    published = _parse_date(
        _first_text(entry, "pubDate")
        or _first_text(entry, "published")
        or _first_text(entry, "updated")
        or _first_text(entry, "dc:date")
    )
    raw_category = _clean_text(_first_text(entry, "category")) or (source.categories[0] if source.categories else "")
    return NewsItem(
        title=title,
        source_name=source.name,
        source_tier=source.normalized_tier,
        source_reliability_weight=source.reliability_weight,
        url=url,
        published_at=published,
        fetched_at=fetched_at,
        summary=summary,
        raw_category=raw_category,
    )


def _rss_items(root: ET.Element) -> tuple[ET.Element, ...]:
    return tuple(element for element in root.iter() if _local_name(element.tag) == "item")


def _atom_entries(root: ET.Element) -> tuple[ET.Element, ...]:
    return tuple(element for element in root.iter() if _local_name(element.tag) == "entry")


def _first_text(entry: ET.Element, local_name: str) -> str:
    target = local_name.split(":", 1)[-1]
    for child in entry.iter():
        if child is entry:
            continue
        if _local_name(child.tag) == target and child.text:
            return child.text
    return ""


def _atom_link(entry: ET.Element) -> str:
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href
    return ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1]


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", value)
    decoded = html.unescape(without_tags)
    return re.sub(r"\s+", " ", decoded).strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
