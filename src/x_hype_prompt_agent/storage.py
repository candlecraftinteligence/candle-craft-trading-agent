from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import DEFAULT_DATABASE_PATH
from .models import NewsItem, ScoredItem, parse_iso_datetime, to_iso, utc_now
from .normalizer import normalize_news_item, title_similarity

SCHEMA_VERSION = 1


class StorageError(RuntimeError):
    """Raised when X hype prompt agent storage cannot be read or written."""


def connect_database(path: Path | str = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    database_path = Path(path)
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection
    except sqlite3.Error as exc:
        raise StorageError(f"Unable to open X hype prompt agent database: {database_path}") from exc
    except OSError as exc:
        raise StorageError(f"Unable to prepare X hype prompt agent database directory: {database_path}") from exc


def initialize_database(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS news_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_tier INTEGER NOT NULL,
                url TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                summary TEXT NOT NULL,
                published_at TEXT,
                fetched_at TEXT NOT NULL,
                raw_category TEXT NOT NULL,
                content_hash TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS ix_news_items_canonical_url ON news_items(canonical_url);
            CREATE INDEX IF NOT EXISTS ix_news_items_normalized_title ON news_items(normalized_title);
            CREATE INDEX IF NOT EXISTS ix_news_items_content_hash ON news_items(content_hash);
            CREATE INDEX IF NOT EXISTS ix_news_items_published_at ON news_items(published_at);

            CREATE TABLE IF NOT EXISTS scored_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
                engagement_score INTEGER NOT NULL,
                hype_score INTEGER NOT NULL,
                market_impact_score INTEGER NOT NULL,
                narrative_score INTEGER NOT NULL,
                controversy_score INTEGER NOT NULL,
                freshness_score INTEGER NOT NULL,
                source_quality_score INTEGER NOT NULL,
                duplicate_penalty INTEGER NOT NULL,
                risk_penalty INTEGER NOT NULL,
                final_score INTEGER NOT NULL,
                category TEXT NOT NULL,
                narratives_json TEXT NOT NULL,
                score_explanation TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS ix_scored_items_news_item_id ON scored_items(news_item_id);
            CREATE INDEX IF NOT EXISTS ix_scored_items_final_score ON scored_items(final_score);

            CREATE TABLE IF NOT EXISTS sent_prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
                scored_item_id INTEGER NOT NULL REFERENCES scored_items(id) ON DELETE CASCADE,
                telegram_message_id INTEGER,
                prompt_text TEXT NOT NULL,
                telegram_text TEXT NOT NULL,
                final_score INTEGER NOT NULL,
                sent_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS ix_sent_prompts_sent_at ON sent_prompts(sent_at);
            CREATE INDEX IF NOT EXISTS ix_sent_prompts_news_item_id ON sent_prompts(news_item_id);

            CREATE TABLE IF NOT EXISTS rejected_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
                reason TEXT NOT NULL,
                score_snapshot_json TEXT NOT NULL,
                rejected_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS ix_rejected_items_news_item_id ON rejected_items(news_item_id);

            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                mode TEXT NOT NULL,
                items_fetched INTEGER NOT NULL DEFAULT 0,
                items_scored INTEGER NOT NULL DEFAULT 0,
                prompts_sent INTEGER NOT NULL DEFAULT 0,
                items_rejected INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL DEFAULT '[]'
            );

            PRAGMA user_version = 1;
            """
        )
        connection.commit()
    except sqlite3.Error as exc:
        raise StorageError("Unable to initialize X hype prompt agent database schema.") from exc


def open_initialized_database(path: Path | str = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    connection = connect_database(path)
    try:
        initialize_database(connection)
        return connection
    except Exception:
        connection.close()
        raise


class XHypeStorage:
    def __init__(self, path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        self.path = Path(path)

    def store_news_item(self, item: NewsItem) -> int:
        normalized = normalize_news_item(item)
        with open_initialized_database(self.path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO news_items (
                    normalized_id, title, normalized_title, source_name, source_tier,
                    url, canonical_url, summary, published_at, fetched_at,
                    raw_category, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.normalized_id,
                    normalized.title,
                    normalized.normalized_title,
                    normalized.source_name,
                    normalized.source_tier,
                    normalized.url,
                    normalized.canonical_url,
                    normalized.summary,
                    to_iso(normalized.published_at),
                    to_iso(normalized.fetched_at) or to_iso(utc_now()),
                    normalized.raw_category,
                    normalized.content_hash,
                ),
            )
            row = connection.execute(
                "SELECT id FROM news_items WHERE normalized_id = ?",
                (normalized.normalized_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise StorageError("Unable to store news item.")
        return int(row["id"])

    def store_scored_item(self, news_item_id: int, scored: ScoredItem) -> int:
        now = to_iso(utc_now())
        with open_initialized_database(self.path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO scored_items (
                    news_item_id, engagement_score, hype_score, market_impact_score,
                    narrative_score, controversy_score, freshness_score,
                    source_quality_score, duplicate_penalty, risk_penalty, final_score,
                    category, narratives_json, score_explanation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    news_item_id,
                    scored.engagement_score,
                    scored.hype_score,
                    scored.market_impact_score,
                    scored.narrative_score,
                    scored.controversy_score,
                    scored.freshness_score,
                    scored.source_quality_score,
                    scored.duplicate_penalty,
                    scored.risk_penalty,
                    scored.final_score,
                    scored.category,
                    json.dumps(list(scored.narratives), sort_keys=True),
                    scored.explanation,
                    now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def store_sent_prompt(
        self,
        *,
        news_item_id: int,
        scored_item_id: int,
        telegram_message_id: int | None,
        prompt_text: str,
        telegram_text: str,
        final_score: int,
        sent_at: datetime | None = None,
    ) -> int:
        with open_initialized_database(self.path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO sent_prompts (
                    news_item_id, scored_item_id, telegram_message_id, prompt_text,
                    telegram_text, final_score, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    news_item_id,
                    scored_item_id,
                    telegram_message_id,
                    prompt_text,
                    telegram_text,
                    final_score,
                    to_iso(sent_at or utc_now()),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def store_rejected_item(
        self,
        *,
        news_item_id: int,
        reason: str,
        score_snapshot: dict[str, Any],
        rejected_at: datetime | None = None,
    ) -> int:
        with open_initialized_database(self.path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO rejected_items (
                    news_item_id, reason, score_snapshot_json, rejected_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    news_item_id,
                    reason,
                    json.dumps(score_snapshot, sort_keys=True),
                    to_iso(rejected_at or utc_now()),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def start_agent_run(self, *, mode: str, started_at: datetime | None = None) -> int:
        with open_initialized_database(self.path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_runs (started_at, mode, errors_json)
                VALUES (?, ?, '[]')
                """,
                (to_iso(started_at or utc_now()), mode),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_agent_run(
        self,
        run_id: int,
        *,
        items_fetched: int,
        items_scored: int,
        prompts_sent: int,
        items_rejected: int,
        errors: tuple[str, ...] = (),
        finished_at: datetime | None = None,
    ) -> None:
        with open_initialized_database(self.path) as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET finished_at = ?,
                    items_fetched = ?,
                    items_scored = ?,
                    prompts_sent = ?,
                    items_rejected = ?,
                    errors_json = ?
                WHERE id = ?
                """,
                (
                    to_iso(finished_at or utc_now()),
                    items_fetched,
                    items_scored,
                    prompts_sent,
                    items_rejected,
                    json.dumps(list(errors), sort_keys=True),
                    run_id,
                ),
            )
            connection.commit()

    def count_sent_since(self, since: datetime) -> int:
        with open_initialized_database(self.path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM sent_prompts WHERE sent_at >= ?",
                (to_iso(since),),
            ).fetchone()
        return int(row["count"] if row else 0)

    def count_sent_today(self, *, now: datetime | None = None) -> int:
        reference = now or utc_now()
        reference = reference if reference.tzinfo else reference.replace(tzinfo=UTC)
        start = reference.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.count_sent_since(start)

    def recently_sent_duplicate(
        self,
        item: NewsItem,
        *,
        duplicate_window_days: int,
        now: datetime | None = None,
    ) -> bool:
        normalized = normalize_news_item(item)
        since = (now or utc_now()) - timedelta(days=max(1, duplicate_window_days))
        with open_initialized_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT n.canonical_url, n.normalized_title, n.content_hash
                FROM sent_prompts sp
                JOIN news_items n ON n.id = sp.news_item_id
                WHERE sp.sent_at >= ?
                """,
                (to_iso(since),),
            ).fetchall()
        for row in rows:
            if normalized.canonical_url and normalized.canonical_url == row["canonical_url"]:
                return True
            if normalized.content_hash and normalized.content_hash == row["content_hash"]:
                return True
            if title_similarity(normalized.normalized_title, row["normalized_title"]) >= 0.9:
                return True
        return False

    def recent_sent_fingerprints(
        self,
        *,
        duplicate_window_days: int,
        now: datetime | None = None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        since = (now or utc_now()) - timedelta(days=max(1, duplicate_window_days))
        with open_initialized_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT n.normalized_title, n.canonical_url
                FROM sent_prompts sp
                JOIN news_items n ON n.id = sp.news_item_id
                WHERE sp.sent_at >= ?
                """,
                (to_iso(since),),
            ).fetchall()
        titles = tuple(str(row["normalized_title"]) for row in rows if row["normalized_title"])
        urls = tuple(str(row["canonical_url"]) for row in rows if row["canonical_url"])
        return titles, urls

    def get_news_item_row(self, news_item_id: int) -> sqlite3.Row | None:
        with open_initialized_database(self.path) as connection:
            return connection.execute("SELECT * FROM news_items WHERE id = ?", (news_item_id,)).fetchone()

    def latest_agent_run(self) -> sqlite3.Row | None:
        with open_initialized_database(self.path) as connection:
            return connection.execute("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()


def row_to_news_item(row: sqlite3.Row) -> NewsItem:
    return NewsItem(
        title=str(row["title"]),
        normalized_title=str(row["normalized_title"]),
        source_name=str(row["source_name"]),
        source_tier=int(row["source_tier"]),
        url=str(row["url"]),
        canonical_url=str(row["canonical_url"]),
        summary=str(row["summary"]),
        published_at=parse_iso_datetime(row["published_at"]),
        fetched_at=parse_iso_datetime(row["fetched_at"]) or utc_now(),
        raw_category=str(row["raw_category"]),
        content_hash=str(row["content_hash"]),
        normalized_id=str(row["normalized_id"]),
    )
