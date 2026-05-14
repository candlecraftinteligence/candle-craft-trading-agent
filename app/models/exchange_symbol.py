from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ExchangeSymbol(TimestampMixin, Base):
    __tablename__ = "exchange_symbols"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", "market_type"),
        Index("ix_exchange_symbols_exchange_market_type", "exchange", "market_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    base_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    quote_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    market_type: Mapped[str] = mapped_column(String(32), default="spot", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    lot_size: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
