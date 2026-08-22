from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.analytics.performance_memory import (
    apply_performance_memory_to_result,
    empty_performance_memory,
)
from app.analytics.symbol_health import SymbolHealthRecord, SymbolPriorityPlan
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerSymbolResult,
)
from app.storage import save_symbol_health_records
from app.universe.symbol_universe import (
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
    BINANCE_USDT_PERP_TOP_VOLUME_MODE,
    SymbolUniverse,
    UniverseResolutionError,
    build_symbol_universe_from_market_caps,
    build_symbol_universe_from_tickers,
    fetch_coinpaprika_market_cap_rankings,
    resolve_symbol_universe,
)
from scripts import run_scan

GENERATED_AT = "2026-08-22T00:00:00Z"


def _asset(rank: int, *, symbol: str | None = None, asset_id: str | None = None) -> dict:
    base_symbol = symbol or f"A{rank}"
    return {
        "id": asset_id or f"asset-{rank}",
        "symbol": base_symbol,
        "rank": rank,
        "quotes": {"USD": {"market_cap": str(1_000_000 - rank)}},
    }


def _ticker(base_symbol: str, *, quote_volume: str = "100") -> dict:
    return {"symbol": f"{base_symbol}USDT", "quoteVolume": quote_volume}


def _contract(base_symbol: str, **updates: object) -> dict:
    contract = {
        "symbol": f"{base_symbol}USDT",
        "baseAsset": base_symbol,
        "quoteAsset": "USDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
        "underlyingType": "COIN",
    }
    contract.update(updates)
    return contract


def _strict_resolution(
    provider_assets: list[dict],
    matched_bases: list[str],
    *,
    universe_size: int,
    contracts: list[dict] | None = None,
) -> SymbolUniverse:
    return build_symbol_universe_from_market_caps(
        [_ticker(symbol) for symbol in matched_bases],
        provider_assets,
        exchange_info={"symbols": contracts or [_contract(symbol) for symbol in matched_bases]},
        universe_size=universe_size,
        generated_at=GENERATED_AT,
    )


def _ranked_assets(count: int) -> list[dict]:
    return [_asset(rank) for rank in range(1, count + 1)]


def _bases(ranks: range | list[int] | tuple[int, ...]) -> list[str]:
    return [f"A{rank}" for rank in ranks]


def _strict_universe(symbols: tuple[str, ...]) -> SymbolUniverse:
    return SymbolUniverse(
        mode=BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
        requested_size=len(symbols),
        resolved_symbols=symbols,
        excluded_symbols=(),
        source="test-market-cap",
        generated_at=GENERATED_AT,
        market_cap_rank_by_symbol={symbol: index for index, symbol in enumerate(symbols, start=1)},
        diagnostics={
            "requested_universe_size": len(symbols),
            "final_universe_count": len(symbols),
            "cache_used": False,
        },
    )


def _lifecycle_record(symbol: str, state: SetupLifecycleState, *, lifecycle_id: str) -> SetupLifecycleRecord:
    return SetupLifecycleRecord.model_validate(
        {
            "lifecycle_id": lifecycle_id,
            "symbol": symbol,
            "mode": "swing",
            "direction": "long",
            "current_state": state,
            "previous_state": None,
            "first_seen_at": "2026-08-22T00:00:00+00:00",
            "last_seen_at": "2026-08-22T00:00:00+00:00",
            "last_transition_at": "2026-08-22T00:00:00+00:00",
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "95",
            "tp1": "110",
            "tp2": "115",
            "tp3": "120",
            "rr": "3",
            "quality_grade_current": "A",
            "actionability_state": "A_GRADE_ACTIONABLE",
            "failed_gate": NA,
            "invalidation_reason": "A close beyond the stored stop invalidates this plan.",
            "invalidation_logic": "A close beyond the stored stop invalidates this plan.",
            "setup_identity": f"{symbol}|swing|long|{lifecycle_id}",
        }
    )


def test_top_50_is_an_absolute_global_rank_boundary() -> None:
    universe = _strict_resolution(
        _ranked_assets(70),
        _bases(range(1, 71)),
        universe_size=50,
    )

    assert len(universe.resolved_symbols) == 50
    assert "A51USDT" not in universe.resolved_symbols
    assert max(universe.market_cap_rank_by_symbol.values()) == 50
    assert universe.diagnostics["rank_gt_n_excluded_count"] == 20


def test_top_100_is_an_absolute_global_rank_boundary() -> None:
    universe = _strict_resolution(
        _ranked_assets(124),
        _bases(range(1, 125)),
        universe_size=100,
    )

    assert len(universe.resolved_symbols) == 100
    assert "A101USDT" not in universe.resolved_symbols
    assert max(universe.market_cap_rank_by_symbol.values()) == 100
    assert universe.diagnostics["rank_gt_n_excluded_count"] == 24


def test_top_50_does_not_backfill_when_only_31_top_50_assets_match() -> None:
    matched_ranks = [*range(1, 32), *range(51, 71)]
    universe = _strict_resolution(
        _ranked_assets(70),
        _bases(matched_ranks),
        universe_size=50,
    )

    assert len(universe.resolved_symbols) == 31
    assert max(universe.market_cap_rank_by_symbol.values()) == 31
    assert not set(_bases(range(51, 71))).intersection(
        symbol.removesuffix("USDT") for symbol in universe.resolved_symbols
    )


def test_top_100_does_not_backfill_when_only_74_top_100_assets_match() -> None:
    matched_ranks = [*range(1, 75), *range(101, 125)]
    universe = _strict_resolution(
        _ranked_assets(124),
        _bases(matched_ranks),
        universe_size=100,
    )

    assert len(universe.resolved_symbols) == 74
    assert max(universe.market_cap_rank_by_symbol.values()) == 74
    assert universe.diagnostics["binance_perp_match_count"] == 74


@pytest.mark.parametrize(("max_symbols", "expected_count"), ((50, 50), (100, 74)))
def test_max_symbols_is_only_a_cap(
    monkeypatch: pytest.MonkeyPatch,
    max_symbols: int,
    expected_count: int,
) -> None:
    universe = _strict_resolution(
        _ranked_assets(100),
        _bases(range(1, 75)),
        universe_size=100,
    )

    async def fake_resolve(*_args: object, **_kwargs: object) -> SymbolUniverse:
        return universe

    monkeypatch.setattr(run_scan, "resolve_symbol_universe", fake_resolve)
    args = run_scan.parse_args(
        [
            "--universe",
            BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
            "--universe-size",
            "100",
            "--max-symbols",
            str(max_symbols),
            "--include-symbols",
            "OUTSIDEUSDT",
        ]
    )

    resolution = asyncio.run(run_scan._resolve_universe_watchlist(args))

    assert len(resolution.symbols) == expected_count
    assert "OUTSIDEUSDT" not in resolution.symbols
    assert resolution.membership_boundary_ignored_symbols == ("OUTSIDEUSDT",)
    assert resolution.universe.diagnostics["requested_max_symbols"] == max_symbols
    assert resolution.universe.diagnostics["final_universe_count"] == expected_count


def test_generic_n_37_uses_the_same_absolute_rank_rule() -> None:
    universe = _strict_resolution(
        _ranked_assets(45),
        _bases(range(1, 46)),
        universe_size=37,
    )

    assert len(universe.resolved_symbols) == 37
    assert max(universe.market_cap_rank_by_symbol.values()) == 37
    assert "A38USDT" not in universe.resolved_symbols


def test_truncated_provider_ranking_fails_instead_of_under_scanning() -> None:
    with pytest.raises(UniverseResolutionError, match="incomplete ranking"):
        _strict_resolution(
            _ranked_assets(50),
            _bases(range(1, 51)),
            universe_size=100,
        )


def test_missing_malformed_and_non_positive_ranks_are_excluded() -> None:
    provider_assets = [
        {"id": "missing", "symbol": "MISS", "rank": None},
        {"id": "malformed", "symbol": "BAD", "rank": "not-a-rank"},
        {"id": "zero", "symbol": "ZERO", "rank": 0},
        _asset(2, symbol="GOOD", asset_id="good"),
        _asset(4, symbol="OVER", asset_id="over"),
    ]
    universe = _strict_resolution(
        provider_assets,
        ["MISS", "BAD", "ZERO", "GOOD", "OVER"],
        universe_size=2,
    )

    assert universe.resolved_symbols == ("GOODUSDT",)
    assert universe.diagnostics["missing_rank_count"] == 1
    assert universe.diagnostics["invalid_rank_count"] == 2
    assert universe.diagnostics["rank_gt_n_excluded_count"] == 1


def test_duplicate_provider_ticker_is_rejected_as_ambiguous() -> None:
    provider_assets = [
        _asset(1, symbol="DUP", asset_id="dup-first"),
        _asset(2, symbol="DUP", asset_id="dup-second"),
        _asset(3, symbol="BTC", asset_id="btc-bitcoin"),
    ]
    universe = _strict_resolution(
        provider_assets,
        ["DUP", "BTC"],
        universe_size=3,
    )

    assert universe.resolved_symbols == ("BTCUSDT",)
    assert universe.diagnostics["ambiguous_symbol_count"] == 1
    assert universe.diagnostics["ambiguous_symbols"] == ["DUP"]


def test_exchange_contract_metadata_rejects_non_crypto_and_non_perpetual_products() -> None:
    provider_assets = [
        _asset(1, symbol="AAPL", asset_id="aapl-crypto-collision"),
        _asset(2, symbol="INDEX", asset_id="index-crypto-collision"),
        _asset(3, symbol="BTC", asset_id="btc-bitcoin"),
    ]
    contracts = [
        _contract("AAPL", contractType="TRADIFI_PERPETUAL", underlyingType="STOCK"),
        _contract("INDEX", underlyingType="INDEX"),
        _contract("BTC"),
    ]
    universe = _strict_resolution(
        provider_assets,
        ["AAPL", "INDEX", "BTC"],
        universe_size=3,
        contracts=contracts,
    )

    assert universe.resolved_symbols == ("BTCUSDT",)
    assert universe.diagnostics["contract_metadata_used"] is True
    assert universe.diagnostics["non_perpetual_contract_excluded_count"] == 1
    assert universe.diagnostics["non_crypto_contract_excluded_count"] == 1


def test_resolution_order_is_deterministic_for_the_same_provider_assets() -> None:
    provider_assets = [
        _asset(5, symbol="FIVE"),
        _asset(1, symbol="ONE"),
        _asset(3, symbol="THREE"),
    ]
    matched = ["ONE", "THREE", "FIVE"]

    first = _strict_resolution(provider_assets, matched, universe_size=5)
    second = _strict_resolution(list(reversed(provider_assets)), list(reversed(matched)), universe_size=5)

    assert first.resolved_symbols == second.resolved_symbols == (
        "ONEUSDT",
        "THREEUSDT",
        "FIVEUSDT",
    )


@pytest.mark.parametrize(
    ("handler", "expected"),
    (
        (lambda request: httpx.Response(429, request=request), "rate limited"),
        (lambda request: httpx.Response(503, request=request), "HTTP 503"),
        (lambda request: httpx.Response(200, content=b"{", request=request), "malformed JSON"),
        (lambda request: httpx.Response(200, json=[], request=request), "empty response"),
    ),
)
def test_coinpaprika_http_and_payload_failures_are_safe(handler, expected: str) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://coinpaprika.test",
        ) as client:
            with pytest.raises(UniverseResolutionError, match=expected):
                await fetch_coinpaprika_market_cap_rankings(http_client=client)

    asyncio.run(scenario())


def test_coinpaprika_timeout_is_a_safe_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("mocked timeout", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://coinpaprika.test",
        ) as client:
            with pytest.raises(UniverseResolutionError, match="timed out"):
                await fetch_coinpaprika_market_cap_rankings(http_client=client)

    asyncio.run(scenario())


def test_binance_availability_failure_is_wrapped_and_does_not_fall_open() -> None:
    async def scenario() -> None:
        def fail_tickers() -> list[dict]:
            raise RuntimeError("Binance unavailable")

        with pytest.raises(UniverseResolutionError, match="Binance USDT perpetual source failed"):
            await resolve_symbol_universe(
                BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
                universe_size=1,
                market_cap_fetcher=lambda: [_asset(1, symbol="BTC")],
                ticker_fetcher=fail_tickers,
                exchange_info_fetcher=lambda: {"symbols": [_contract("BTC")]},
            )

    asyncio.run(scenario())


def test_active_lifecycle_history_is_preserved_but_cannot_expand_strict_membership(tmp_path) -> None:
    db_path = tmp_path / "lifecycle.sqlite"
    outside_states = (
        SetupLifecycleState.WATCHLISTED,
        SetupLifecycleState.STALKING,
        SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.CONFIRMED,
    )
    outside_symbols = tuple(f"OUT{index}USDT" for index in range(len(outside_states)))
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(
            _lifecycle_record("INUSDT", SetupLifecycleState.WATCHLISTED, lifecycle_id="inside")
        )
        for index, (symbol, state) in enumerate(zip(outside_symbols, outside_states, strict=True)):
            repository.upsert_record(
                _lifecycle_record(symbol, state, lifecycle_id=f"outside-{index}")
            )

    args = SimpleNamespace(lifecycle=True, database_path=db_path, max_symbols=1)
    base = run_scan.WatchlistResolution(
        symbols=("INUSDT",),
        source_label="strict top-n",
        universe=_strict_universe(("INUSDT",)),
    )

    first = run_scan._watchlist_with_lifecycle_priority(args, base)
    restarted = run_scan._watchlist_with_lifecycle_priority(args, base)

    assert first.symbols == restarted.symbols == ("INUSDT",)
    assert first.active_lifecycle_symbols == ("INUSDT",)
    assert set(first.lifecycle_membership_ignored_symbols) == set(outside_symbols)
    assert first.lifecycle_priority_added_symbols == ()
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        assert len(repository.list_records_for_symbol(symbol="INUSDT")) == 1
        for symbol in outside_symbols:
            assert len(repository.list_records_for_symbol(symbol=symbol)) == 1


def test_cooldown_symbol_health_and_adaptive_priority_cannot_inject_membership(tmp_path) -> None:
    db_path = tmp_path / "health.sqlite"
    save_symbol_health_records(
        db_path,
        {
            "INUSDT": SymbolHealthRecord(symbol="INUSDT", current_health_score=60),
            "OUTUSDT": SymbolHealthRecord(
                symbol="OUTUSDT",
                current_health_score=99,
                cooldown_until="2099-01-01T00:00:00+00:00",
            ),
        },
    )
    args = SimpleNamespace(
        lifecycle=False,
        database_path=db_path,
        max_symbols=1,
        adaptive_symbol_priority=True,
        watch=True,
        universe_size=1,
    )
    watchlist = run_scan.WatchlistResolution(
        symbols=("INUSDT",),
        source_label="strict top-n",
        universe=_strict_universe(("INUSDT",)),
    )

    plan = run_scan._symbol_priority_plan_for_watchlist(args, watchlist)
    queued = run_scan._queued_symbols_for_scan(args, watchlist, plan)

    assert plan.original_symbols == ("INUSDT",)
    assert queued == ("INUSDT",)
    assert "OUTUSDT" not in plan.priority_by_symbol()


def test_final_queue_defense_rejects_any_out_of_membership_priority_extra() -> None:
    args = SimpleNamespace()
    watchlist = run_scan.WatchlistResolution(
        symbols=("INUSDT",),
        source_label="strict top-n",
        universe=_strict_universe(("INUSDT",)),
    )
    malformed_plan = SymbolPriorityPlan(
        enabled=True,
        original_symbols=("INUSDT",),
        symbols_to_scan=("OUTUSDT", "INUSDT"),
    )

    assert run_scan._queued_symbols_for_scan(args, watchlist, malformed_plan) == ("INUSDT",)


def test_performance_memory_only_annotates_existing_scan_results() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="INUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
    )
    config = ScannerRunConfig.model_validate(
        {
            "symbols": ["INUSDT"],
            "exchange": "binance",
            "account_equity": Decimal("1000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )
    result = ScannerRunResult(
        config=config,
        results=(symbol_result,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )

    updated = apply_performance_memory_to_result(result, empty_performance_memory())

    assert tuple(item.symbol for item in updated.results) == ("INUSDT",)


def test_persisted_watch_and_continue_candidates_are_intersections_not_additions(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = run_scan.WatchlistResolution(
        symbols=("INUSDT",),
        source_label="strict top-n",
        universe=_strict_universe(("INUSDT",)),
    )
    args = SimpleNamespace(
        watch_only_near_misses=False,
        exclude_symbols=(),
        max_symbols=1,
        min_quote_volume=Decimal("0"),
        database_path=tmp_path / "unused.sqlite",
    )
    monkeypatch.setattr(run_scan, "load_symbols_from_run", lambda *_args, **_kwargs: ("OUTUSDT", "INUSDT"))

    latest = run_scan._resolve_watchlist_from_latest_run(args, membership_boundary=boundary)

    assert latest.symbols == ("INUSDT",)
    assert latest.membership_boundary_ignored_symbols == ("OUTUSDT",)

    monkeypatch.setattr(run_scan, "load_watch_state", lambda *_args, **_kwargs: SimpleNamespace(symbols={}))
    monkeypatch.setattr(run_scan, "state_watch_symbols", lambda *_args, **_kwargs: ("OUTUSDT",))
    monkeypatch.setattr(run_scan, "LATEST_RUN_PATH", tmp_path / "missing-latest.json")
    continued = run_scan._extend_watchlist_for_continue_watch(args, boundary)

    assert continued.symbols == ("INUSDT",)
    assert continued.membership_boundary_ignored_symbols == ("OUTUSDT",)


def test_non_market_cap_universe_include_behavior_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    universe = build_symbol_universe_from_tickers(
        BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        [_ticker("BASE")],
        universe_size=1,
        generated_at=GENERATED_AT,
    )

    async def fake_resolve(*_args: object, **_kwargs: object) -> SymbolUniverse:
        return universe

    monkeypatch.setattr(run_scan, "resolve_symbol_universe", fake_resolve)
    args = run_scan.parse_args(
        [
            "--universe",
            BINANCE_USDT_PERP_TOP_VOLUME_MODE,
            "--universe-size",
            "1",
            "--include-symbols",
            "EXTRAUSDT",
        ]
    )

    resolution = asyncio.run(run_scan._resolve_universe_watchlist(args))

    assert resolution.symbols == ("BASEUSDT", "EXTRAUSDT")
