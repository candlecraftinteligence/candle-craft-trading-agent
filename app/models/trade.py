from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class Trade(TimestampMixin, Base):
    __tablename__ = "trades"
    __table_args__ = (Index("ix_trades_symbol_status_opened", "exchange_symbol_id", "status", "opened_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    trade_idea_id: Mapped[int | None] = mapped_column(ForeignKey("trade_ideas.id"))
    mode: Mapped[str] = mapped_column(String(32), default="paper", nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)
    execution_source: Mapped[str] = mapped_column(String(64), default="manual_or_paper", nullable=False)
    risk_warning: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
