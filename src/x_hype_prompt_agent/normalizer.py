from __future__ import annotations

import hashlib
import re
import string
from dataclasses import replace
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import NewsItem

TRACKING_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_name",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_src",
    "cmp",
}

NOISE_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "over",
    "the",
    "this",
    "to",
    "with",
    "crypto",
    "cryptocurrency",
    "market",
    "markets",
    "news",
    "update",
    "latest",
}

ASSET_NORMALIZATIONS = (
    (re.compile(r"\bbitcoin\b", re.IGNORECASE), "btc"),
    (re.compile(r"\bbtc\b", re.IGNORECASE), "btc"),
    (re.compile(r"\bethereum\b", re.IGNORECASE), "eth"),
    (re.compile(r"\beth\b", re.IGNORECASE), "eth"),
    (re.compile(r"\bsolana\b", re.IGNORECASE), "sol"),
    (re.compile(r"\bsol\b", re.IGNORECASE), "sol"),
)


def normalize_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text

    scheme = (parsed.scheme or "https").lower()
    hostname = (parsed.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    netloc = hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() not in TRACKING_QUERY_PARAMS and not key.lower().startswith("utm_")
    ]
    query = urlencode(sorted(query_pairs))
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_title(title: str) -> str:
    text = (title or "").lower()
    for pattern, replacement in ASSET_NORMALIZATIONS:
        text = pattern.sub(replacement, text)
    text = text.translate(str.maketrans({char: " " for char in string.punctuation}))
    text = re.sub(r"\s+", " ", text).strip()
    words = [word for word in text.split(" ") if word and word not in NOISE_WORDS]
    return " ".join(words)


def content_hash_for(item: NewsItem) -> str:
    source = "|".join(
        (
            normalize_title(item.title),
            normalize_url(item.url),
            normalize_title(item.summary)[:400],
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def normalized_id_for(item: NewsItem) -> str:
    canonical_url = normalize_url(item.url)
    title = normalize_title(item.title)
    basis = canonical_url or title or item.title.strip().lower()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def normalize_news_item(item: NewsItem) -> NewsItem:
    canonical_url = normalize_url(item.url)
    normalized_title = normalize_title(item.title)
    normalized = replace(
        item,
        canonical_url=canonical_url,
        normalized_title=normalized_title,
    )
    return replace(
        normalized,
        normalized_id=normalized_id_for(normalized),
        content_hash=content_hash_for(normalized),
    )


def title_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def deduplicate_items(items: list[NewsItem] | tuple[NewsItem, ...], *, similarity_threshold: float = 0.9) -> tuple[NewsItem, ...]:
    unique: list[NewsItem] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    seen_hashes: set[str] = set()

    for raw_item in items:
        item = normalize_news_item(raw_item)
        if item.canonical_url and item.canonical_url in seen_urls:
            continue
        if item.normalized_title and item.normalized_title in seen_titles:
            continue
        if item.content_hash and item.content_hash in seen_hashes:
            continue
        if any(title_similarity(item.normalized_title, existing.normalized_title) >= similarity_threshold for existing in unique):
            continue

        unique.append(item)
        if item.canonical_url:
            seen_urls.add(item.canonical_url)
        if item.normalized_title:
            seen_titles.add(item.normalized_title)
        if item.content_hash:
            seen_hashes.add(item.content_hash)

    return tuple(unique)
