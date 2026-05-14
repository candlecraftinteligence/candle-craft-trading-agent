from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class Catalyst(TimestampMixin, Base):
    __tablename__ = "catalysts"
    __table_args__ = (Index("ix_catalysts_asset_published", "asset_id", "published_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"))
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str | None] = mapped_column(String(512))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    catalyst_type: Mapped[str] = mapped_column(String(64), default="N/A", nullable=False)
    sentiment: Mapped[str] = mapped_column(String(32), default="N/A", nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), default="Unverified", nullable=False)
    verification_status: Mapped[str] = mapped_column(String(32), default="Unverified", nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict | None] = mapped_column(JSON)
