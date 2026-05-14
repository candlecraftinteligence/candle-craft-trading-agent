from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, JSON, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class DerivativesSnapshot(TimestampMixin, Base):
    __tablename__ = "derivatives_snapshots"
    __table_args__ = (Index("ix_derivatives_snapshots_symbol_captured", "exchange_symbol_id", "captured_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open_interest: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    funding_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    next_funding_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    long_short_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    basis: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    data_quality: Mapped[str] = mapped_column(String(32), default="Unverified", nullable=False)
    raw_payload: Mapped[dict | None] = mapped_column(JSON)
