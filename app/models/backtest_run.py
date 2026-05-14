from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class BacktestRun(TimestampMixin, Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (Index("ix_backtest_runs_strategy_status", "strategy_name", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(32), default="N/A", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parameters: Mapped[dict | None] = mapped_column(JSON)
    asset_universe: Mapped[list[str] | None] = mapped_column(JSON)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    code_version: Mapped[str | None] = mapped_column(String(64))
