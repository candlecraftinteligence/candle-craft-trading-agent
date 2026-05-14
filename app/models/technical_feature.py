from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class TechnicalFeature(TimestampMixin, Base):
    __tablename__ = "technical_features"
    __table_args__ = (Index("ix_technical_features_symbol_timeframe_calculated", "exchange_symbol_id", "timeframe", "calculated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_candle_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rsi_14: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    ema_20: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    ema_50: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    macd: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    macd_signal: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    atr_14: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    realized_volatility: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    trend_label: Mapped[str] = mapped_column(String(32), default="N/A", nullable=False)
    data_quality: Mapped[str] = mapped_column(String(32), default="Unverified", nullable=False)
