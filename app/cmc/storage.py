"""Bounded dedicated sidecar. Never opens or migrates the CCI runtime database."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.cmc.models import encoded, stable_id

APPLICATION_ID = 0x434D4331


class Sidecar:
    def __init__(self, path: str | Path, *, max_bytes: int = 64 * 1024 * 1024,
                 max_records: int = 20000, retention_days: int = 30):
        self.path = Path(path).resolve()
        if not self.path.name.startswith("cmc_") or self.path.suffix not in {".sqlite", ".db"}:
            raise ValueError("CMC sidecar must have a cmc_ filename and .sqlite/.db extension")
        if max_bytes < 65536 or max_records < 1 or retention_days < 1:
            raise ValueError("invalid sidecar limits")
        self.max_bytes, self.max_records, self.retention_days = max_bytes, max_records, retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            application_id = db.execute("PRAGMA application_id").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if application_id != APPLICATION_ID and (application_id or tables):
                raise ValueError("refusing non-CMC database")
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={max_bytes // page_size}")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_at TEXT,
                    acquired_at TEXT NOT NULL, decision_at TEXT, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS evidence_causal ON evidence(kind, acquired_at, source_at);
                CREATE INDEX IF NOT EXISTS evidence_decision ON evidence(kind, decision_at);
                CREATE INDEX IF NOT EXISTS evidence_retention ON evidence(acquired_at,id);
                CREATE TABLE IF NOT EXISTS usage (
                    bucket TEXT PRIMARY KEY, credits INTEGER NOT NULL, requests INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS storage_stats (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), records INTEGER NOT NULL, bytes INTEGER NOT NULL);
                INSERT OR IGNORE INTO storage_stats VALUES(1,0,0);
                CREATE TRIGGER IF NOT EXISTS evidence_added AFTER INSERT ON evidence BEGIN
                    UPDATE storage_stats SET records=records+1,bytes=bytes+length(CAST(NEW.payload AS BLOB)) WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS evidence_removed AFTER DELETE ON evidence BEGIN
                    UPDATE storage_stats SET records=records-1,bytes=bytes-length(CAST(OLD.payload AS BLOB)) WHERE singleton=1;
                END;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.1)
        try:
            # DELETE journal and small transactions bound extra disk use; no persistent WAL.
            db.execute("PRAGMA busy_timeout=100")
            page_size=db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={self.max_bytes // page_size}")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, kind: str, payload, *, acquired_at: datetime,
            source_at: datetime | None = None, decision_at: datetime | None = None,
            identity: str | None = None) -> str:
        if any(t is not None and t.tzinfo is None for t in (acquired_at,source_at,decision_at)):
            raise ValueError("sidecar timestamps must include timezone")
        acquired_at = acquired_at.astimezone(UTC)
        source_at = source_at.astimezone(UTC) if source_at else None
        decision_at = decision_at.astimezone(UTC) if decision_at else None
        body = encoded(payload)
        if len(body.encode()) > min(2 * 1024 * 1024, self.max_bytes // 4):
            raise ValueError("sidecar record exceeds payload budget")
        record_id = identity or stable_id((kind, body))
        cutoff = (acquired_at - timedelta(days=self.retention_days)).isoformat()
        with self.connect() as db:
            existing=db.execute("SELECT kind,payload FROM evidence WHERE id=?", (record_id,)).fetchone()
            if existing:
                if existing!=(kind,body):
                    raise ValueError("conflicting immutable CMC evidence identity")
                return record_id
            db.execute("DELETE FROM evidence WHERE id IN (SELECT id FROM evidence WHERE acquired_at < ? LIMIT 1000)", (cutoff,))
            count, used = db.execute("SELECT records,bytes FROM storage_stats WHERE singleton=1").fetchone()
            # Leave half the page budget for indexes, accounting, B-tree slack, and
            # the bounded rollback journal. Evict in indexed batches, not table scans.
            while count >= self.max_records or used + len(body.encode()) > self.max_bytes // 2:
                if count == 0:
                    raise ValueError("sidecar storage budget exhausted")
                db.execute("DELETE FROM evidence WHERE id IN (SELECT id FROM evidence ORDER BY acquired_at,id LIMIT 32)")
                count, used = db.execute("SELECT records,bytes FROM storage_stats WHERE singleton=1").fetchone()
            db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?)", (
                record_id, kind, source_at.isoformat() if source_at else None,
                acquired_at.isoformat(), decision_at.isoformat() if decision_at else None, body))
        return record_id

    def causal(self, kind: str, as_of: datetime, *, limit: int = 100):
        if as_of.tzinfo is None or limit < 1:
            raise ValueError("causal query requires timezone and positive limit")
        as_of = as_of.astimezone(UTC)
        with self.connect() as db:
            records = db.execute("""SELECT payload FROM evidence WHERE kind=? AND acquired_at<=?
                AND (source_at IS NULL OR source_at<=?) ORDER BY acquired_at DESC,id LIMIT ?""",
                (kind, as_of.isoformat(), as_of.isoformat(), min(limit, self.max_records))).fetchall()
        return [json.loads(row[0]) for row in records]

    def reserve(self, at: datetime, credits: int, *, daily: int, monthly: int, record_request: bool = True) -> bool:
        if credits < 0 or daily < 0 or monthly < 0:
            raise ValueError("negative credit budget")
        at = at.astimezone(UTC)
        buckets = (("d:" + at.strftime("%Y-%m-%d"), daily), ("m:" + at.strftime("%Y-%m"), monthly))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, maximum in buckets:
                current = db.execute("SELECT credits FROM usage WHERE bucket=?", (key,)).fetchone()
                if (current[0] if current else 0) + credits > maximum:
                    return False
            for key, _ in buckets:
                db.execute("INSERT INTO usage VALUES(?,?,?) ON CONFLICT(bucket) DO UPDATE SET credits=credits+excluded.credits,requests=requests+excluded.requests", (key, credits, int(record_request)))
            # Keep at most 400 days / 24 months of accounting, independently of evidence retention.
            db.execute("DELETE FROM usage WHERE bucket LIKE 'd:%' AND bucket < ?", ("d:" + (at - timedelta(days=400)).strftime("%Y-%m-%d"),))
            db.execute("DELETE FROM usage WHERE bucket LIKE 'm:%' AND bucket < ?", ("m:" + (at - timedelta(days=730)).strftime("%Y-%m"),))
        return True

    def usage(self, at: datetime):
        at = at.astimezone(UTC)
        with self.connect() as db:
            return {key: {"reserved_credits": credits, "requests": requests}
                    for key, credits, requests in db.execute("SELECT * FROM usage WHERE bucket IN (?,?)",
                        ("d:" + at.strftime("%Y-%m-%d"), "m:" + at.strftime("%Y-%m")))}
