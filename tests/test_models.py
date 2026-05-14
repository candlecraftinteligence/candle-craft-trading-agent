from __future__ import annotations

from app.db.base import Base
import app.models as models


def test_model_imports() -> None:
    expected_models = [
        "Asset",
        "ExchangeSymbol",
        "Candle",
        "MarketSnapshot",
        "DerivativesSnapshot",
        "TechnicalFeature",
        "Catalyst",
        "TradeIdea",
        "Alert",
        "Trade",
        "JournalEntry",
        "BacktestRun",
    ]

    for model_name in expected_models:
        assert hasattr(models, model_name)


def test_model_metadata_contains_required_tables() -> None:
    required_tables = {
        "assets",
        "exchange_symbols",
        "candles",
        "market_snapshots",
        "derivatives_snapshots",
        "technical_features",
        "catalysts",
        "trade_ideas",
        "alerts",
        "trades",
        "journal_entries",
        "backtest_runs",
    }

    assert required_tables.issubset(Base.metadata.tables.keys())
