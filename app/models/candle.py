from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class Candle(TimestampMixin, Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("exchange_symbol_id", "timeframe", "opened_at"),
        Index("ix_candles_symbol_timeframe_opened", "exchange_symbol_id", "timeframe", "opened_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    quote_volume: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    trades_count: Mapped[int | None] = mapped_column(Integer)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_quality: Mapped[str] = mapped_column(String(32), default="Unverified", nullable=False)
