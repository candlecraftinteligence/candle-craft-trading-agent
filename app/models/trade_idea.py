from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class TradeIdea(TimestampMixin, Base):
    __tablename__ = "trade_ideas"
    __table_args__ = (Index("ix_trade_ideas_symbol_status", "exchange_symbol_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(32), default="N/A", nullable=False)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    invalidation: Mapped[str] = mapped_column(Text, nullable=False)
    risk_warning: Mapped[str] = mapped_column(Text, nullable=False)
    entry_zone_low: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    entry_zone_high: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    stop_reference: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="system", nullable=False)
    data_quality: Mapped[str] = mapped_column(String(32), default="Unverified", nullable=False)
