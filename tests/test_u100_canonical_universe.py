"""Canonical Top-100 genuine-crypto universe regressions."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.analytics.public_alert_funnel import DEFAULT_PUBLIC_RR_MIN
from app.analytics.public_signal_quality import (
    MIN_PUBLIC_SETUP_QUALITY_SCORE,
    MIN_PUBLIC_SIGNAL_GRADE,
)
from app.core.minimum_rr import SWING_HARD_MINIMUM_RR
from app.lifecycle.eligibility import PUBLIC_WATCHLIST_MIN_RR
from app.lifecycle.owner_monitoring import select_tracking_obligations
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import CONFIRMED_MIN_RR
from app.universe.canonical_qualification import (
    CANONICAL_QUALIFIED_COHORT_LIMIT,
    EXCHANGE_CONTRACT_PRICE_BASIS,
)
from app.universe.symbol_universe import (
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
    UniverseResolutionError,
    build_symbol_universe_from_market_caps,
    resolve_symbol_universe,
)
from scripts import run_scan
from tests.test_p5a_active_owner_monitoring import _seed_entered
from tests.test_strict_market_cap_universe import (
    GENERATED_AT,
    _asset,
    _contract,
    _strict_resolution,
    _ticker,
)


def test_non_genuine_assets_inside_raw_top_100_do_not_backfill_the_qualified_cohort() -> None:
    assets = [
        _asset(1, symbol="USDT", asset_id="usdt-tether", name="Tether", classified=False),
        _asset(2, symbol="WBTC", asset_id="wbtc-wrapped-bitcoin", name="Wrapped Bitcoin", classified=False),
        _asset(3, symbol="DEFI", asset_id="defi-protocol", name="DeFi Artificial Intelligence Protocol"),
    ]
    assets.extend(
        _asset(rank, symbol=f"C{rank}", asset_id=f"coin-{rank}", name=f"Coin {rank}")
        for rank in range(4, 104)
    )
    matched = ["DEFI", *[f"C{rank}" for rank in range(4, 104) if rank != 50]]
    universe = _strict_resolution(assets, matched, universe_size=100)

    assert universe.diagnostics["qualified_cohort_size"] == 100
    assert universe.diagnostics["qualified_cohort_limit"] == 100
    assert "USDTUSDT" not in universe.resolved_symbols
    assert "WBTCUSDT" not in universe.resolved_symbols
    assert "DEFIUSDT" in universe.resolved_symbols
    assert "C102USDT" in universe.resolved_symbols
    assert "C103USDT" not in universe.resolved_symbols
    assert "C50USDT" not in universe.resolved_symbols
    assert len(universe.resolved_symbols) == 99
    assert max(universe.diagnostics["qualified_rank_by_symbol"].values()) == 100
    categories = {item["provider_id"]: item["category"] for item in universe.diagnostics["qualification_exclusions"]}
    assert categories["usdt-tether"] == "stablecoin"
    assert categories["wbtc-wrapped-bitcoin"] == "wrapped_or_bridged"


def test_universe_size_above_100_cannot_extend_the_qualified_cohort() -> None:
    assets = [_asset(rank, symbol=f"C{rank}", asset_id=f"coin-{rank}") for rank in range(1, 131)]
    universe = _strict_resolution(
        assets,
        [f"C{rank}" for rank in range(1, 131)],
        universe_size=250,
    )

    assert CANONICAL_QUALIFIED_COHORT_LIMIT == 100
    assert len(universe.resolved_symbols) == 100
    assert "C100USDT" in universe.resolved_symbols
    assert "C101USDT" not in universe.resolved_symbols
    assert universe.diagnostics["qualified_cohort_limit"] == 100
    assert universe.diagnostics["rank_gt_n_excluded_count"] == 30


@pytest.mark.parametrize(
    ("provider_id", "symbol", "name"),
    (
        ("hype-hyperliquid", "HYPE", "Hyperliquid"),
        ("arb-arbitrum", "ARB", "Arbitrum"),
        ("grt-the-graph", "GRT", "The Graph"),
        ("pump-pumpfun", "PUMP", "Pump.fun"),
        ("doge-dogecoin", "DOGE", "Dogecoin"),
        ("jup-jupiter-exchange-token", "JUP", "Jupiter Exchange Token"),
        ("sky-sky", "SKY", "Sky"),
        ("trump-official-trump", "TRUMP", "OFFICIAL TRUMP"),
    ),
)
def test_verified_provider_identity_survives_a_ticker_collision(provider_id: str, symbol: str, name: str) -> None:
    assets = [
        _asset(1, symbol=symbol, asset_id=provider_id, name=name),
        _asset(2, symbol=symbol, asset_id=f"{provider_id}-other", name=f"Other {name}"),
        _asset(3, symbol="BTC", asset_id="btc-bitcoin", name="Bitcoin"),
    ]
    universe = _strict_resolution(assets, [symbol, "BTC"], universe_size=3)
    membership = {
        item["provider_id"]: item for item in universe.diagnostics["qualified_membership"]
    }

    assert universe.resolved_symbols.count(f"{symbol}USDT") == 1
    assert membership[provider_id]["collision_status"] == "verified_disambiguation"
    assert membership[provider_id]["contract_multiplier"] == 1
    assert membership[provider_id]["price_quantity_basis"] == EXCHANGE_CONTRACT_PRICE_BASIS
    rejected = {
        item["provider_id"]: item["reason"] for item in universe.diagnostics["identity_rejections"]
    }
    assert rejected[f"{provider_id}-other"] == "unresolved_ambiguity"


def test_unresolved_ticker_collision_is_excluded_with_an_explicit_reason() -> None:
    assets = [
        _asset(1, symbol="ARB", asset_id="arb-fake-a", name="Arb One"),
        _asset(2, symbol="ARB", asset_id="arb-fake-b", name="Arb Two"),
        _asset(3, symbol="BTC", asset_id="btc-bitcoin", name="Bitcoin"),
    ]
    universe = _strict_resolution(assets, ["ARB", "BTC"], universe_size=3)

    assert universe.resolved_symbols == ("BTCUSDT",)
    assert universe.diagnostics["ambiguous_symbols"] == ["ARB"]
    reasons = {item["provider_id"]: item["reason"] for item in universe.diagnostics["identity_rejections"]}
    assert reasons == {"arb-fake-a": "unresolved_ambiguity", "arb-fake-b": "unresolved_ambiguity"}


def test_verified_1000_prefix_mapping_preserves_exchange_units() -> None:
    contract = _contract("1000SHIB")
    contract["filters"] = [
        {"filterType": "PRICE_FILTER", "tickSize": "0.000001"},
        {"filterType": "LOT_SIZE", "stepSize": "1"},
    ]
    universe = build_symbol_universe_from_market_caps(
        [_ticker("1000SHIB")],
        [_asset(1, symbol="SHIB", asset_id="shib-shiba-inu", name="Shiba Inu")],
        exchange_info={"symbols": [contract]},
        universe_size=1,
        generated_at=GENERATED_AT,
    )
    member = universe.diagnostics["qualified_membership"][0]

    assert universe.resolved_symbols == ("1000SHIBUSDT",)
    assert "SHIBUSDT" not in universe.resolved_symbols
    assert member["contract_multiplier"] == 1000
    assert member["exchange_base_asset"] == "1000SHIB"
    assert member["price_quantity_basis"] == EXCHANGE_CONTRACT_PRICE_BASIS
    assert member["tick_size"] == "0.000001"
    assert member["step_size"] == "1"
    assert member["collision_status"] == "verified_unique"


def test_invalid_multiplier_is_rejected_and_does_not_backfill() -> None:
    scaled = _contract("1000SHIB", contractMultiplier=100)
    universe = build_symbol_universe_from_market_caps(
        [_ticker("1000SHIB"), _ticker("BTC"), _ticker("TAIL")],
        [
            _asset(1, symbol="SHIB", asset_id="shib-shiba-inu", name="Shiba Inu"),
            _asset(2, symbol="BTC", asset_id="btc-bitcoin", name="Bitcoin"),
            _asset(3, symbol="TAIL", asset_id="tail-coin", name="Tail"),
        ],
        exchange_info={"symbols": [scaled, _contract("BTC"), _contract("TAIL")]},
        universe_size=2,
        generated_at=GENERATED_AT,
    )

    assert universe.resolved_symbols == ("BTCUSDT",)
    assert "1000SHIBUSDT" not in universe.resolved_symbols
    assert "SHIBUSDT" not in universe.resolved_symbols
    assert "TAILUSDT" not in universe.resolved_symbols
    reasons = {item["provider_id"]: item["reason"] for item in universe.diagnostics["identity_rejections"]}
    assert reasons["shib-shiba-inu"] == "invalid_multiplier"


def test_unknown_inside_raw_top_30_does_not_stop_verified_discovery() -> None:
    assets = [_asset(rank, symbol=f"C{rank}", asset_id=f"coin-{rank}") for rank in range(1, 28)]
    assets.append(
        _asset(28, symbol="RAIN", asset_id="rain-rain-protocol", name="Rain Protocol", classified=False)
    )
    assets.extend(_asset(rank, symbol=f"C{rank}", asset_id=f"coin-{rank}") for rank in range(29, 32))
    universe = _strict_resolution(
        assets,
        [f"C{rank}" for rank in range(1, 32)] + ["RAIN"],
        universe_size=30,
    )

    assert "RAINUSDT" not in universe.resolved_symbols
    assert "C27USDT" in universe.resolved_symbols
    assert "C29USDT" in universe.resolved_symbols
    assert "C30USDT" in universe.resolved_symbols
    assert "C31USDT" not in universe.resolved_symbols
    assert universe.diagnostics["selected_count"] == 29
    assert universe.diagnostics["withheld_unknown_count"] == 1
    withheld = {item["provider_id"]: item["category"] for item in universe.diagnostics["withheld_identities"]}
    assert withheld == {"rain-rain-protocol": "unclassified"}
    assert universe.diagnostics["qualified_rank_by_symbol"]["C27USDT"] == 27
    assert "C29USDT" not in universe.diagnostics["qualified_rank_by_symbol"]
    assert universe.diagnostics["qualified_rank_bounds_by_symbol"]["C29USDT"] == {"lower": 28, "upper": 29}


def test_unclassified_ticker_twin_blocks_symbol_only_mapping() -> None:
    universe = _strict_resolution(
        [
            _asset(1, symbol="CC", asset_id="cc-canton-network", classified=False),
            _asset(2, symbol="CC", asset_id="cc-cloudchat", classified=False),
            _asset(3, symbol="BTC", asset_id="btc-bitcoin"),
        ],
        ["CC", "BTC"],
        universe_size=3,
    )

    assert universe.resolved_symbols == ("BTCUSDT",)
    assert "CCUSDT" not in universe.resolved_symbols
    reasons = {item["provider_id"]: item["reason"] for item in universe.diagnostics["identity_rejections"]}
    assert reasons["cc-canton-network"] == "unresolved_ambiguity"
    withheld = {item["provider_id"]: item["category"] for item in universe.diagnostics["withheld_identities"]}
    assert withheld["cc-cloudchat"] == "unclassified"


def test_genuine_asset_beyond_the_conservative_boundary_is_not_selected() -> None:
    universe = _strict_resolution(
        [
            _asset(1, symbol="RAIN", asset_id="rain-rain-protocol", classified=False),
            _asset(2, symbol="BTC", asset_id="btc-bitcoin"),
            _asset(3, symbol="ETH", asset_id="eth-ethereum"),
        ],
        ["RAIN", "BTC", "ETH"],
        universe_size=2,
    )

    assert universe.resolved_symbols == ("BTCUSDT",)
    assert "RAINUSDT" not in universe.resolved_symbols
    assert "ETHUSDT" not in universe.resolved_symbols


def test_representation_metadata_excludes_wrapped_asset_without_using_its_name() -> None:
    wrapped = _asset(1, symbol="PLAIN", asset_id="plain-not-registered", name="Plain Asset", classified=False)
    wrapped["tags"] = ["wrapped-token"]
    universe = _strict_resolution(
        [wrapped, _asset(2, symbol="BTC", asset_id="btc-bitcoin", name="Bitcoin")],
        ["PLAIN", "BTC"],
        universe_size=1,
    )

    assert universe.resolved_symbols == ("BTCUSDT",)
    categories = {item["provider_id"]: item["category"] for item in universe.diagnostics["qualification_exclusions"]}
    assert categories["plain-not-registered"] == "wrapped_or_bridged"


def test_sector_tags_do_not_exclude_a_reviewed_protocol_token() -> None:
    eigen = _asset(2, symbol="EIGEN", asset_id="eigen-eigenlayer", name="EigenLayer", classified=False)
    eigen["tags"] = ["liquid-staking-tokens-lst", "restaking", "defi"]
    receipt = _asset(1, symbol="STETH", asset_id="steth-lido-staked-ether", name="Lido Staked Ether", classified=False)
    sector_only = _asset(3, symbol="LST", asset_id="lst-sector-only", name="Liquid Staking Sector", classified=False)
    sector_only["tags"] = ["liquid-staking-tokens-lst", "defi"]
    universe = _strict_resolution(
        [receipt, eigen, sector_only],
        ["STETH", "EIGEN", "LST"],
        universe_size=2,
    )

    assert universe.resolved_symbols == ("EIGENUSDT",)
    assert "LSTUSDT" not in universe.resolved_symbols
    categories = {item["provider_id"]: item["category"] for item in universe.diagnostics["qualification_exclusions"]}
    assert categories["steth-lido-staked-ether"] == "liquid_staked_or_restaked"
    withheld = {item["provider_id"]: item["category"] for item in universe.diagnostics["withheld_identities"]}
    assert withheld["lst-sector-only"] == "unclassified"


def test_asset_level_classification_conflict_is_withheld() -> None:
    universe = _strict_resolution(
        [
            _asset(1, symbol="BTC", asset_id="btc-bitcoin", qualification_class="stablecoin"),
            _asset(2, symbol="ETH", asset_id="eth-ethereum"),
        ],
        ["BTC", "ETH"],
        universe_size=2,
    )
    wrapped = _strict_resolution(
        [
            _asset(1, symbol="WBTC", asset_id="wbtc-wrapped-bitcoin", qualification_class="genuine"),
            _asset(2, symbol="ETH", asset_id="eth-ethereum"),
        ],
        ["WBTC", "ETH"],
        universe_size=2,
    )

    assert universe.resolved_symbols == ("ETHUSDT",)
    assert wrapped.resolved_symbols == ("ETHUSDT",)
    withheld = {item["provider_id"]: item["category"] for item in universe.diagnostics["withheld_identities"]}
    assert withheld["btc-bitcoin"] == "ambiguous"
    wrapped_withheld = {item["provider_id"]: item["category"] for item in wrapped.diagnostics["withheld_identities"]}
    assert wrapped_withheld["wbtc-wrapped-bitcoin"] == "ambiguous"
    conflict = _asset(1, symbol="BOTH", asset_id="both-unregistered", classified=False)
    conflict["tags"] = ["cryptocurrency", "stablecoin"]
    both = _strict_resolution(
        [conflict, _asset(2, symbol="ETH", asset_id="eth-ethereum")],
        ["BOTH", "ETH"],
        universe_size=2,
    )
    assert both.resolved_symbols == ("ETHUSDT",)
    both_withheld = {item["provider_id"]: item["category"] for item in both.diagnostics["withheld_identities"]}
    assert both_withheld["both-unregistered"] == "ambiguous"


def test_cryptocurrency_category_metadata_is_positive_evidence() -> None:
    native = _asset(1, symbol="NATIVE", asset_id="native-unregistered", classified=False)
    native["tags"] = ["cryptocurrency", "defi"]
    universe = _strict_resolution([native], ["NATIVE"], universe_size=1)

    assert universe.resolved_symbols == ("NATIVEUSDT",)


def test_explicit_synthetic_classification_does_not_backfill() -> None:
    universe = _strict_resolution(
        [
            _asset(1, symbol="SYN", asset_id="syn-basket", qualification_class="synthetic"),
            _asset(2, symbol="BTC", asset_id="btc-bitcoin"),
            _asset(3, symbol="TAIL", asset_id="tail-coin"),
        ],
        ["SYN", "BTC", "TAIL"],
        universe_size=1,
    )

    assert universe.resolved_symbols == ("BTCUSDT",)
    assert "TAILUSDT" not in universe.resolved_symbols
    categories = {item["provider_id"]: item["category"] for item in universe.diagnostics["qualification_exclusions"]}
    assert categories["syn-basket"] == "synthetic"


def test_stale_explicit_classification_fails_closed() -> None:
    with pytest.raises(UniverseResolutionError, match="stale ranking"):
        _strict_resolution(
            [
                _asset(
                    1,
                    symbol="NEW",
                    asset_id="new-unregistered",
                    qualification_class="genuine",
                    last_updated="2026-08-21T00:00:00Z",
                )
            ],
            ["NEW"],
            universe_size=1,
        )


def test_corrupt_ranking_keeps_owner_monitoring_available() -> None:
    from app.lifecycle.owner_monitoring import discovery_failure_continues_owner_monitoring

    with pytest.raises(UniverseResolutionError, match="stale ranking") as raised:
        _strict_resolution(
            [_asset(1, symbol="BTC", asset_id="btc-bitcoin", last_updated="2026-08-21T00:00:00Z")],
            ["BTC"],
            universe_size=1,
        )

    assert discovery_failure_continues_owner_monitoring(raised.value) is True


def test_low_quote_volume_does_not_backfill_from_above_the_cohort() -> None:
    universe = build_symbol_universe_from_market_caps(
        [
            _ticker("LOW", quote_volume="1"),
            _ticker("HIGH", quote_volume="100"),
            _ticker("TAIL", quote_volume="100000"),
        ],
        [
            _asset(1, symbol="LOW", asset_id="low-coin"),
            _asset(2, symbol="HIGH", asset_id="high-coin"),
            _asset(3, symbol="TAIL", asset_id="tail-coin"),
        ],
        exchange_info={"symbols": [_contract("LOW"), _contract("HIGH"), _contract("TAIL")]},
        universe_size=2,
        min_quote_volume=Decimal("10"),
        generated_at=GENERATED_AT,
    )

    assert universe.resolved_symbols == ("HIGHUSDT",)
    assert universe.diagnostics["below_min_quote_volume_count"] == 1
    assert "TAILUSDT" not in universe.resolved_symbols


def test_stale_missing_and_duplicate_identity_evidence_fail_closed() -> None:
    with pytest.raises(UniverseResolutionError, match="stale ranking"):
        _strict_resolution(
            [_asset(1, symbol="BTC", asset_id="btc-bitcoin", last_updated="2026-08-21T00:00:00Z")],
            ["BTC"],
            universe_size=1,
        )
    with pytest.raises(UniverseResolutionError, match="incomplete ranking"):
        _strict_resolution(
            [_asset(1, symbol="BTC", asset_id="btc-bitcoin", last_updated=None)],
            ["BTC"],
            universe_size=1,
        )
    with pytest.raises(UniverseResolutionError, match="duplicate provider identity"):
        _strict_resolution(
            [
                _asset(1, symbol="BTC", asset_id="btc-bitcoin"),
                _asset(2, symbol="ETH", asset_id="btc-bitcoin"),
            ],
            ["BTC", "ETH"],
            universe_size=1,
        )


def test_provider_outage_fails_closed() -> None:
    async def scenario() -> None:
        def fail_rankings() -> list[dict[str, object]]:
            raise RuntimeError("coinpaprika down")

        with pytest.raises(UniverseResolutionError, match="market-cap source failed"):
            await resolve_symbol_universe(
                BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
                universe_size=1,
                market_cap_fetcher=fail_rankings,
                ticker_fetcher=lambda: [_ticker("BTC")],
                exchange_info_fetcher=lambda: {"symbols": [_contract("BTC")]},
                generated_at=GENERATED_AT,
            )

    asyncio.run(scenario())


def test_active_lifecycle_outside_discovery_continues_without_new_membership(tmp_path) -> None:
    universe = _strict_resolution(
        [_asset(1, symbol="BTC", asset_id="btc-bitcoin", name="Bitcoin")],
        ["BTC"],
        universe_size=1,
    )
    db_path = tmp_path / "lifecycle.sqlite"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-out", symbol="OUTUSDT")
        obligations = select_tracking_obligations(repository)
        stored = repository.get_record_by_lifecycle_id(entered.lifecycle_id)

    assert entered.lifecycle_id in {item.lifecycle_id for item in obligations}
    assert stored is not None and stored.symbol == "OUTUSDT"
    assert "OUTUSDT" not in universe.resolved_symbols

    watchlist = run_scan.WatchlistResolution(
        symbols=universe.resolved_symbols,
        source_label="qualified top-100",
        universe=universe,
    )
    prioritized = run_scan._watchlist_with_lifecycle_priority(
        type("Args", (), {"lifecycle": True, "database_path": db_path, "max_symbols": None})(),
        watchlist,
    )

    assert prioritized.symbols == ("BTCUSDT",)
    assert "OUTUSDT" not in prioritized.symbols
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        assert repository.get_record_by_lifecycle_id(entered.lifecycle_id) is not None


def test_public_tracking_thresholds_are_unchanged() -> None:
    assert DEFAULT_PUBLIC_RR_MIN == Decimal("3")
    assert PUBLIC_WATCHLIST_MIN_RR == Decimal("3")
    assert CONFIRMED_MIN_RR == Decimal("3")
    assert SWING_HARD_MINIMUM_RR == Decimal("2.5")
    assert MIN_PUBLIC_SIGNAL_GRADE == "A"
    assert MIN_PUBLIC_SETUP_QUALITY_SCORE == Decimal("88")
