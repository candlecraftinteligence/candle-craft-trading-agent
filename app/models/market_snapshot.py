from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, JSON, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class MarketSnapshot(TimestampMixin, Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_market_snapshots_symbol_captured", "exchange_symbol_id", "captured_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    bid: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    volume_24h: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    quote_volume_24h: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    change_pct_24h: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    data_quality: Mapped[str] = mapped_column(String(32), default="Unverified", nullable=False)
    raw_payload: Mapped[dict | None] = mapped_column(JSON)
