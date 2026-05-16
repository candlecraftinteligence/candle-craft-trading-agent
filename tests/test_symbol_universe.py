from __future__ import annotations

import asyncio
from decimal import Decimal

from app.pipeline.scanner_runner import ScannerRunConfig, ScannerRunResult
from app.universe.symbol_universe import (
    BINANCE_USDM_24H_TICKER_SOURCE,
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
    BINANCE_USDT_PERP_TOP_TRADABLE_MODE,
    BINANCE_USDT_PERP_TOP_VOLUME_MODE,
    COINPAPRIKA_MARKET_CAP_SOURCE,
    UniverseResolutionError,
    build_symbol_universe_from_market_caps,
    build_symbol_universe_from_tickers,
    resolve_symbol_universe,
)
from scripts import run_scan

GENERATED_AT = "2026-05-16T00:00:00Z"


class CapturingUniverseScannerRunner:
    configs = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def run(self, config):
        self.__class__.configs.append(config)
        return ScannerRunResult(
            config=config,
            results=(),
            scanned_symbols=0,
            failed_symbols=0,
            trade_ideas_created=0,
            dry_run_alerts_created=0,
            journal_entries_created=0,
        )


def test_symbol_universe_sorts_by_quote_volume_descending() -> None:
    universe = build_symbol_universe_from_tickers(
        BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        [
            {"symbol": "ETHUSDT", "quoteVolume": "100"},
            {"symbol": "BTCUSDT", "quoteVolume": "300"},
            {"symbol": "SOLUSDT", "quoteVolume": "200"},
        ],
        universe_size=3,
        generated_at=GENERATED_AT,
    )

    assert universe.resolved_symbols == ("BTCUSDT", "SOLUSDT", "ETHUSDT")
    assert [item.symbol for item in universe.top_by_quote_volume()] == ["BTCUSDT", "SOLUSDT", "ETHUSDT"]


def test_tradable_universe_excludes_stablecoin_and_leveraged_pairs() -> None:
    universe = build_symbol_universe_from_tickers(
        BINANCE_USDT_PERP_TOP_TRADABLE_MODE,
        [
            {"symbol": "USDCUSDT", "quoteVolume": "1000"},
            {"symbol": "BTCUPUSDT", "quoteVolume": "900"},
            {"symbol": "BTCDOWNUSDT", "quoteVolume": "800"},
            {"symbol": "JUPUSDT", "quoteVolume": "700"},
            {"symbol": "BTCUSDT", "quoteVolume": "600"},
        ],
        universe_size=10,
        generated_at=GENERATED_AT,
    )

    assert universe.resolved_symbols == ("JUPUSDT", "BTCUSDT")
    assert "USDCUSDT" in universe.excluded_symbols
    assert "BTCUPUSDT" in universe.excluded_symbols
    assert "BTCDOWNUSDT" in universe.excluded_symbols


def test_universe_filters_to_usdt_symbols_only() -> None:
    universe = build_symbol_universe_from_tickers(
        BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        [
            {"symbol": "BTCUSDC", "quoteVolume": "1000"},
            {"symbol": "ETHUSDT", "quoteVolume": "500"},
        ],
        universe_size=10,
        generated_at=GENERATED_AT,
    )

    assert universe.resolved_symbols == ("ETHUSDT",)
    assert universe.excluded_symbols == ("BTCUSDC",)


def test_universe_size_limits_resolved_symbols_after_sorting() -> None:
    universe = build_symbol_universe_from_tickers(
        BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        [
            {"symbol": "BTCUSDT", "quoteVolume": "300"},
            {"symbol": "ETHUSDT", "quoteVolume": "200"},
            {"symbol": "SOLUSDT", "quoteVolume": "100"},
        ],
        universe_size=2,
        generated_at=GENERATED_AT,
    )

    assert universe.requested_size == 2
    assert universe.resolved_symbols == ("BTCUSDT", "ETHUSDT")


def test_market_cap_universe_intersects_public_rankings_with_binance_usdt_perps() -> None:
    universe = build_symbol_universe_from_market_caps(
        [
            {"symbol": "BTCUSDT", "quoteVolume": "300"},
            {"symbol": "ETHUSDT", "quoteVolume": "200"},
            {"symbol": "SOLUSDT", "quoteVolume": "100"},
            {"symbol": "USDCUSDT", "quoteVolume": "90"},
            {"symbol": "XRPUSDC", "quoteVolume": "80"},
        ],
        [
            {"symbol": "ETH", "rank": 2, "quotes": {"USD": {"market_cap": "2000"}}},
            {"symbol": "BTC", "rank": 1, "quotes": {"USD": {"market_cap": "3000"}}},
            {"symbol": "XRP", "rank": 3, "quotes": {"USD": {"market_cap": "1000"}}},
            {"symbol": "SOL", "rank": 5, "quotes": {"USD": {"market_cap": "500"}}},
            {"symbol": "USDC", "rank": 4, "quotes": {"USD": {"market_cap": "700"}}},
        ],
        universe_size=3,
        generated_at=GENERATED_AT,
    )

    assert universe.mode == BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE
    assert universe.source == COINPAPRIKA_MARKET_CAP_SOURCE
    assert universe.resolved_symbols == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    assert universe.market_cap_rank_by_symbol == {"BTCUSDT": 1, "ETHUSDT": 2, "SOLUSDT": 5}
    assert "USDCUSDT" in universe.excluded_symbols


def test_market_cap_universe_source_failure_returns_clean_universe_error() -> None:
    async def scenario() -> None:
        def ticker_fetcher():
            return [{"symbol": "BTCUSDT", "quoteVolume": "100"}]

        def market_cap_fetcher():
            raise RuntimeError("source unavailable")

        try:
            await resolve_symbol_universe(
                BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
                universe_size=1,
                ticker_fetcher=ticker_fetcher,
                market_cap_fetcher=market_cap_fetcher,
                generated_at=GENERATED_AT,
            )
        except UniverseResolutionError as exc:
            assert str(exc) == "universe_error: market-cap source failed: source unavailable"
        else:
            raise AssertionError("expected UniverseResolutionError")

    asyncio.run(scenario())


def test_manual_symbols_stay_on_existing_watchlist_path() -> None:
    args = run_scan.parse_args(["--symbols", "btcusdt", "ETHUSDT"])
    resolution = run_scan._resolve_watchlist(args)

    assert resolution.source_label == "symbols"
    assert resolution.symbols == ("BTCUSDT", "ETHUSDT")
    assert resolution.universe.mode == "manual"
    assert resolution.universe.resolved_symbols == ("BTCUSDT", "ETHUSDT")


def test_explicit_universe_mode_takes_priority_over_symbols(monkeypatch, capsys) -> None:
    async def fake_resolve_symbol_universe(mode: str, **_kwargs: object):
        return build_symbol_universe_from_tickers(
            mode,
            [
                {"symbol": "BTCUSDT", "quoteVolume": "200"},
                {"symbol": "ETHUSDT", "quoteVolume": "100"},
            ],
            universe_size=2,
            generated_at=GENERATED_AT,
        )

    CapturingUniverseScannerRunner.configs = []
    monkeypatch.setattr(run_scan, "resolve_symbol_universe", fake_resolve_symbol_universe)
    monkeypatch.setattr(run_scan, "ScannerRunner", CapturingUniverseScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "MANUALUSDT",
                "--universe",
                BINANCE_USDT_PERP_TOP_VOLUME_MODE,
                "--universe-size",
                "2",
                "--display",
                "compact",
            ]
        )
    )

    config = CapturingUniverseScannerRunner.configs[0]
    assert [symbol.symbol for symbol in config.symbols] == ["BTCUSDT", "ETHUSDT"]


def test_malformed_or_missing_quote_volume_is_excluded() -> None:
    universe = build_symbol_universe_from_tickers(
        BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        [
            {"symbol": "BTCUSDT", "quoteVolume": "100"},
            {"symbol": "BROKENUSDT", "quoteVolume": "not-a-number"},
            {"symbol": "MISSINGUSDT"},
            {"symbol": "LOWUSDT", "quoteVolume": "49"},
        ],
        universe_size=10,
        min_quote_volume=Decimal("50"),
        generated_at=GENERATED_AT,
    )

    assert universe.resolved_symbols == ("BTCUSDT",)
    assert universe.excluded_symbols == ("BROKENUSDT", "MISSINGUSDT", "LOWUSDT")


def test_json_payload_includes_universe_block() -> None:
    universe = build_symbol_universe_from_tickers(
        BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        [{"symbol": "BTCUSDT", "quoteVolume": "100"}],
        universe_size=50,
        min_quote_volume=Decimal("10"),
        generated_at=GENERATED_AT,
    )
    config = ScannerRunConfig.model_validate(
        {
            "symbols": ["BTCUSDT"],
            "exchange": "binance",
            "account_equity": Decimal("1000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )
    result = ScannerRunResult(
        config=config,
        results=(),
        scanned_symbols=0,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
        resume_metadata={"universe": universe.to_json()},
    )

    payload = run_scan._json_payload(result)

    assert payload["universe"] == {
        "mode": BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        "label": "Top Binance USDT perpetuals by quote volume",
        "requested_size": 50,
        "resolved_symbols": ["BTCUSDT"],
        "excluded_symbols": [],
        "source": BINANCE_USDM_24H_TICKER_SOURCE,
        "generated_at": GENERATED_AT,
        "min_quote_volume": "10",
        "market_cap_rank_by_symbol": {},
        "market_cap_usd_by_symbol": {},
    }
