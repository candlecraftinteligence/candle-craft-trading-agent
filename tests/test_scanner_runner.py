from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from app.agents.alert_agent import AlertAgent
from app.data.dtos import NA
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunner


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _trend_candles_with_valid_setup() -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    for index in range(220):
        price = Decimal(100 + index)
        candles.append(
            {
                "timestamp": index,
                "open": price,
                "high": price + Decimal("1"),
                "low": price - Decimal("1"),
                "close": price,
                "volume": Decimal("100"),
            }
        )

    candles[170]["low"] = Decimal("150")
    candles[180]["high"] = Decimal("350")
    candles[-1]["open"] = Decimal("359")
    candles[-1]["high"] = Decimal("365")
    candles[-1]["low"] = Decimal("149")
    candles[-1]["close"] = Decimal("360")
    candles[-1]["volume"] = Decimal("500")
    return candles


def _flat_candles() -> list[dict[str, Decimal | int]]:
    return [
        {
            "timestamp": index,
            "open": Decimal("100"),
            "high": Decimal("101"),
            "low": Decimal("99"),
            "close": Decimal("100"),
            "volume": Decimal("100"),
        }
        for index in range(220)
    ]


def _bos_without_stop_candles() -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    for index in range(220):
        price = Decimal(100 + index)
        candles.append(
            {
                "timestamp": index,
                "open": price,
                "high": price + Decimal("1"),
                "low": price - Decimal("1"),
                "close": price,
                "volume": Decimal("100"),
            }
        )
    candles[180]["high"] = Decimal("350")
    candles[-1]["open"] = Decimal("359")
    candles[-1]["high"] = Decimal("365")
    candles[-1]["low"] = Decimal("355")
    candles[-1]["close"] = Decimal("360")
    candles[-1]["volume"] = Decimal("500")
    return candles


class FakeExchangeClient:
    def __init__(
        self,
        candles_by_symbol: dict[str, list[dict[str, Decimal | int]]],
        *,
        funding: Decimal | str = Decimal("0.0001"),
        open_interest: Decimal | str = Decimal("105"),
        previous_open_interest: Decimal | str = Decimal("100"),
        failing_symbols: set[str] | None = None,
    ) -> None:
        self.candles_by_symbol = candles_by_symbol
        self.funding = funding
        self.open_interest = open_interest
        self.previous_open_interest = previous_open_interest
        self.failing_symbols = failing_symbols or set()
        self.requested_symbols: list[str] = []

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
        self.requested_symbols.append(symbol)
        if symbol in self.failing_symbols:
            raise RuntimeError(f"mocked kline failure for {symbol}")
        return self.candles_by_symbol[symbol][-limit:]

    async def get_ticker(self, symbol: str) -> dict[str, Decimal | str | int]:
        close = self.candles_by_symbol.get(symbol, _flat_candles())[-1]["close"]
        return {
            "symbol": symbol,
            "last_price": close,
            "price_change_ratio_24h": Decimal("0.01"),
            "quote_volume_24h": Decimal("100000000"),
        }

    async def get_funding_rate(self, symbol: str) -> dict[str, Decimal | str | int]:
        if self.funding == NA:
            raise RuntimeError("funding unavailable")
        return {"symbol": symbol, "funding_rate": self.funding, "timestamp": 1}

    async def get_open_interest(self, symbol: str) -> dict[str, Decimal | str | int]:
        if self.open_interest == NA:
            raise RuntimeError("open interest unavailable")
        return {
            "symbol": symbol,
            "open_interest": self.open_interest,
            "previous_open_interest": self.previous_open_interest,
        }


class SpyAlertAgent(AlertAgent):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    async def send(self, alert: dict[str, Any] | None = None, **overrides: Any) -> Any:
        payload = dict(alert or {})
        payload.update(overrides)
        self.calls.append(payload)
        if payload.get("dry_run") is not True:
            raise AssertionError("scanner alerts must be dry-run by default")
        return await super().send(payload)


def _config(symbols: list[str], **overrides: object) -> ScannerRunConfig:
    data: dict[str, object] = {
        "symbols": symbols,
        "exchange": "binance",
        "interval": "15m",
        "candle_limit": 220,
        "dry_run_alerts": True,
        "account_equity": Decimal("10000"),
        "risk_per_trade_pct": Decimal("1"),
        "min_score_for_idea": Decimal("80"),
    }
    data.update(overrides)
    return ScannerRunConfig.model_validate(data)


def test_scanner_handles_one_valid_mocked_symbol() -> None:
    client = FakeExchangeClient({"BTCUSDT": _trend_candles_with_valid_setup()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.JOURNAL_ENTRY_CREATED
    assert ScannerPipelineStatus.IDEA_CREATED in symbol_result.status_history
    assert ScannerPipelineStatus.ALERT_DRY_RUN_CREATED in symbol_result.status_history
    assert symbol_result.trade_idea is not None
    assert symbol_result.trade_idea.quality_gate_result.passed is True
    assert result.trade_ideas_created == 1


def test_scanner_handles_no_setup() -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert symbol_result.trade_idea is None
    assert "No sweep, BOS, or CHoCH" in str(symbol_result.rejection_reason)


def test_scanner_continues_if_one_symbol_fails() -> None:
    client = FakeExchangeClient(
        {
            "FAILUSDT": _flat_candles(),
            "BTCUSDT": _trend_candles_with_valid_setup(),
        },
        failing_symbols={"FAILUSDT"},
    )

    result = run(ScannerRunner(exchange_client=client).run(_config(["FAILUSDT", "BTCUSDT"])))

    assert result.scanned_symbols == 2
    assert result.results[0].status == ScannerPipelineStatus.FAILED
    assert result.results[0].error_message == "mocked kline failure for FAILUSDT"
    assert result.results[1].status == ScannerPipelineStatus.JOURNAL_ENTRY_CREATED


def test_scanner_rejects_candidate_without_invalidation() -> None:
    client = FakeExchangeClient({"BTCUSDT": _bos_without_stop_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.REJECTED_BY_TECHNICAL
    assert symbol_result.trade_idea is None
    assert "invalidation: N/A" in symbol_result.missing_data


def test_scanner_creates_dry_run_alert_only_when_idea_passes_gates() -> None:
    alert_agent = SpyAlertAgent()
    client = FakeExchangeClient(
        {
            "BTCUSDT": _trend_candles_with_valid_setup(),
            "ETHUSDT": _flat_candles(),
        }
    )

    result = run(ScannerRunner(exchange_client=client, alert_agent=alert_agent).run(_config(["BTCUSDT", "ETHUSDT"])))

    assert result.results[0].alert_result is not None
    assert result.results[0].alert_result.dry_run is True
    assert result.results[1].alert_result is None
    assert len(alert_agent.calls) == 1
    assert alert_agent.calls[0]["dry_run"] is True


def test_scanner_creates_journal_entry_only_when_idea_exists() -> None:
    client = FakeExchangeClient(
        {
            "BTCUSDT": _trend_candles_with_valid_setup(),
            "ETHUSDT": _flat_candles(),
        }
    )

    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT", "ETHUSDT"])))

    assert result.results[0].journal_entry is not None
    assert result.results[1].journal_entry is None
    assert result.journal_entries_created == 1


def test_dry_run_alert_does_not_call_telegram_live() -> None:
    alert_agent = SpyAlertAgent()
    client = FakeExchangeClient({"BTCUSDT": _trend_candles_with_valid_setup()})

    result = run(ScannerRunner(exchange_client=client, alert_agent=alert_agent).run(_config(["BTCUSDT"])))

    assert result.results[0].alert_result is not None
    assert result.results[0].alert_result.status == "dry_run"
    assert alert_agent.calls[0]["dry_run"] is True


def test_missing_funding_marked_na() -> None:
    client = FakeExchangeClient({"BTCUSDT": _trend_candles_with_valid_setup()}, funding=NA)
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.funding_rate == NA
    assert "funding_rate: N/A" in symbol_result.missing_data
    assert symbol_result.derivatives_result is not None
    assert symbol_result.derivatives_result.funding.raw_funding_rate == NA


def test_missing_oi_marked_na() -> None:
    client = FakeExchangeClient({"BTCUSDT": _trend_candles_with_valid_setup()}, open_interest=NA)
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.open_interest == NA
    assert "open_interest: N/A" in symbol_result.missing_data
    assert symbol_result.derivatives_result is not None
    assert symbol_result.derivatives_result.open_interest.current_open_interest == NA


def test_tests_use_mocked_exchange_client_without_live_api_calls() -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    assert client.requested_symbols == ["BTCUSDT"]
