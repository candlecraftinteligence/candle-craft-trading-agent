"""Synthetic rankings exercise the real U100 adapter and exchange intersection."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.cmc.client import Fetch
from app.cmc.collection import IdentityManifest
from app.cmc.endpoints import Endpoint
from app.cmc.identity import AssetBinding
from app.cmc.models import EvidenceState
from app.cmc.universe import normalize_rankings
from app.universe.symbol_universe import (
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE, CMC_MARKET_CAP_SOURCE,
    UniverseResolutionError, build_symbol_universe_from_market_caps, resolve_symbol_universe,
)
from tests.test_strict_market_cap_universe import _contract, _ticker

AT = datetime(2026, 10, 29, 10, tzinfo=UTC)


def _row(cmc_id, rank, symbol, *, tags=()):
    return {"id": cmc_id, "cmc_rank": rank, "symbol": symbol, "slug": f"fixture-{cmc_id}",
            "name": f"Synthetic asset {cmc_id}", "tags": list(tags), "last_updated": AT.isoformat(),
            "quote": [{"symbol": "USD", "id": 2781, "market_cap": str(1000000-rank),
                       "last_updated": AT.isoformat()}]}


def _binding(row, *, category="genuine", multiplier=1):
    base = row["symbol"] if multiplier == 1 else f"{multiplier}{row['symbol']}"
    return AssetBinding(cmc_id=row["id"], cmc_slug=row["slug"], binance_base=base,
        contract_symbol=f"{base}USDT", exchange_id=270, market_id=row["id"], approved=True,
        evidence_ref="synthetic-exact-contract-review", acquired_at=AT-timedelta(days=1),
        reviewed_at=AT-timedelta(hours=2), approved_at=AT-timedelta(hours=1),
        expires_at=AT+timedelta(days=1), owner_approval_ref="synthetic-owner-approval",
        qualification_class=category, contract_multiplier=multiplier)


def _fetch(rows, **updates):
    return Fetch(endpoint=Endpoint.LISTINGS, acquired_at=AT, state=EvidenceState.UNVERIFIED,
        data=rows, raw={"status": {"timestamp": AT.isoformat()}}, raw_ref="fixture-hash").model_copy(update=updates)


def _universe(rows, bindings, bases, *, size=100):
    snapshot = normalize_rankings(_fetch(rows), IdentityManifest(bindings=tuple(bindings)), as_of=AT)
    return build_symbol_universe_from_market_caps([_ticker(base) for base in bases], snapshot.assets,
        exchange_info={"symbols": [_contract(base) for base in bases]}, universe_size=size,
        generated_at=AT.isoformat(), ranking_source=CMC_MARKET_CAP_SOURCE, verified_mappings=snapshot.mappings)


def test_full_100_boundary_unknown_collision_exclusions_and_multiplier():
    rows = [_row(825, 1, "USDT", tags=("stablecoin",)),
            _row(1001, 2, "WRAP", tags=("wrapped-tokens",)),
            _row(2000, 3, "PROTOCOL", tags=("liquid-staking", "restaking", "defi")),
            _row(2001, 4, "UNKNOWN"), _row(2002, 5, "SHIB"), _row(2003, 6, "PROTOCOL")]
    rows += [_row(3000+i, i+7, f"C{i}") for i in range(99)]
    bindings = [_binding(row, multiplier=1000 if row["symbol"] == "SHIB" else 1)
                for row in rows if row["id"] not in {825, 1001, 2001, 2003}]
    bases = [b.binance_base for b in bindings]
    universe = _universe(rows, bindings, bases)
    diagnostics = universe.diagnostics
    assert universe.source == CMC_MARKET_CAP_SOURCE
    assert diagnostics["potential_member_count"] == 100
    assert diagnostics["withheld_unknown_count"] == 2
    assert diagnostics["selected_count"] == 98
    assert "PROTOCOLUSDT" in universe.resolved_symbols
    assert "UNKNOWNUSDT" not in universe.resolved_symbols
    assert "C95USDT" in universe.resolved_symbols and "C96USDT" not in universe.resolved_symbols
    shib = next(m for m in diagnostics["qualified_membership"] if m["provider_id"] == "cmc:2002")
    assert shib["exchange_symbol"] == "1000SHIBUSDT"
    assert shib["contract_multiplier"] == 1000
    assert shib["price_quantity_basis"] == "exchange_contract_unit"
    assert diagnostics["exclusion_counts"]["stablecoin"] == 1
    assert diagnostics["exclusion_counts"]["wrapped_or_bridged"] == 1
    assert all(m["provider_id"].startswith("cmc:") for m in diagnostics["qualified_membership"])


@pytest.mark.parametrize("case", ["stale", "stale_quote", "future", "missing_time", "bad_id", "duplicate_id", "gap", "incomplete"])
def test_bad_or_untrustworthy_ranking_fails_closed(case):
    rows = [_row(1, 1, "BTC"), _row(2, 2, "ETH")]
    bindings = [_binding(row) for row in rows]
    if case == "stale": rows[0]["last_updated"] = (AT-timedelta(hours=2)).isoformat()
    if case == "stale_quote": rows[0]["quote"][0]["last_updated"] = (AT-timedelta(hours=2)).isoformat()
    if case == "future": rows[0]["last_updated"] = (AT+timedelta(seconds=1)).isoformat()
    if case == "missing_time": rows[0].pop("last_updated")
    if case == "bad_id": rows[0]["id"] = "btc-bitcoin"
    if case == "duplicate_id": rows[1]["id"] = 1
    if case == "gap": rows[1]["cmc_rank"] = 3
    if case == "incomplete": rows.pop()
    with pytest.raises(UniverseResolutionError, match="CMC"):
        _universe(rows, bindings, ["BTC", "ETH"], size=2)


@pytest.mark.parametrize("case", ["expired", "slug_mismatch", "unapproved", "missing_classification", "conflicting_representation"])
def test_identity_cannot_be_inferred_from_symbol(case):
    rows = [_row(1, 1, "BTC"), _row(2, 2, "ETH")]
    first = _binding(rows[0])
    if case == "expired": first = first.model_copy(update={"expires_at": AT})
    if case == "slug_mismatch": first = first.model_copy(update={"cmc_slug": "other"})
    if case == "unapproved": first = first.model_copy(update={"approved": False})
    if case == "missing_classification": first = first.model_copy(update={"qualification_class": None})
    if case == "conflicting_representation": rows[0]["tags"] = ["wrapped-tokens"]
    universe = _universe(rows, [first, _binding(rows[1])], ["BTC", "ETH"], size=2)
    assert universe.resolved_symbols == ("ETHUSDT",)
    assert universe.diagnostics["withheld_unknown_count"] == 1


def test_real_captured_v3_quote_shape_keeps_historical_source_time():
    payload = json.loads(Path("tests/fixtures/cmc_authenticated/18_coinmarketcap.json").read_text())
    at = datetime.fromisoformat(payload["status"]["timestamp"].replace("Z", "+00:00"))
    snapshot = normalize_rankings(Fetch(endpoint=Endpoint.LISTINGS, acquired_at=at,
        state=EvidenceState.UNVERIFIED, raw=payload, data=payload["data"]), IdentityManifest(), as_of=at)
    btc = snapshot.assets[0]
    assert btc["id"] == "cmc:1" and btc["symbol"] == "BTC"
    assert Decimal(str(btc["quotes"]["USD"]["market_cap"])) > 0
    assert snapshot.mappings == {}
    with pytest.raises(UniverseResolutionError, match="stale"):
        normalize_rankings(_fetch(payload["data"], acquired_at=at, raw=payload), IdentityManifest(), as_of=AT)


def test_cmc_resolver_uses_exact_verified_mappings_and_never_coinpaprika(monkeypatch):
    from app.cmc import universe as cmc
    from app.universe import symbol_universe as module
    rows = [_row(1, 1, "BTC")]
    snapshot = normalize_rankings(_fetch(rows), IdentityManifest(bindings=(_binding(rows[0]),)), as_of=AT)
    async def fetch(): return snapshot
    async def forbidden(): raise AssertionError("competing ranking must not be queried")
    monkeypatch.setattr(cmc, "fetch_rankings", fetch)
    monkeypatch.setattr(module, "fetch_coinpaprika_market_cap_rankings", forbidden)
    result = asyncio.run(resolve_symbol_universe(BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
        universe_size=1, market_cap_provider="cmc", generated_at=AT.isoformat(),
        ticker_fetcher=lambda: [_ticker("BTC")], exchange_info_fetcher=lambda: {"symbols": [_contract("BTC")]}))
    assert result.resolved_symbols == ("BTCUSDT",) and result.source == CMC_MARKET_CAP_SOURCE
    assert result.diagnostics["qualified_membership"][0]["cmc_id"] == 1
    assert result.diagnostics["cmc_source_timestamp"] == AT.isoformat()


def test_cmc_startup_outage_monitors_owned_plan_on_every_iteration(tmp_path, monkeypatch):
    from app.cmc import universe as cmc
    from scripts import run_scan
    from tests.test_f05_source_cleanup import _arm_owner, _watch_argv, _progress
    from tests.test_f05_discovery_fail_closed import _capture_runner, _table_count
    from tests.test_watch_mode import SequenceWatchRunner
    from tests.test_p5a_monitoring_binding_query_repair import _KlineClient
    from app.lifecycle.repositories import SQLiteSetupLifecycleRepository

    path, owner, before = _arm_owner(tmp_path, monkeypatch)
    calls, monitors = [], []
    async def unavailable():
        calls.append("cmc")
        raise UniverseResolutionError("universe_error: CMC market-cap source unavailable (http_429)")
    sleep = asyncio.sleep
    async def skip_delay(_): await sleep(0)
    monkeypatch.setenv("MARKET_CAP_PROVIDER", "cmc")
    monkeypatch.setattr(cmc, "fetch_rankings", unavailable)
    monkeypatch.setattr(asyncio, "sleep", skip_delay)
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: monitors.append("owned") or _KlineClient())
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    _capture_runner()
    output = tmp_path/"outage.jsonl"
    asyncio.run(run_scan.main(_watch_argv(path, output, iterations=3)))
    summaries = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(summaries) == 3 and all(row["status"] == "FAILED" for row in summaries)
    assert calls == ["cmc"]*4 and monitors == ["owned"]*3
    assert SequenceWatchRunner.configs == []
    assert _table_count(path, "public_alert_events") == 0
    with SQLiteSetupLifecycleRepository(path) as repository:
        progress = _progress(repository, owner.lifecycle_id)
        assert progress.evaluation_cursor_close_at != before
        assert progress.tp1_at is not None
