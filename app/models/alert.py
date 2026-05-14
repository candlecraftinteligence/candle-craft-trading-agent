from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class Alert(TimestampMixin, Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_symbol_status_triggered", "exchange_symbol_id", "status", "triggered_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    trade_idea_id: Mapped[int | None] = mapped_column(ForeignKey("trade_ideas.id"))
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    data_quality: Mapped[str] = mapped_column(String(32), default="Unverified", nullable=False)
