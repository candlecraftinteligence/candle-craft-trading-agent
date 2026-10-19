"""Deterministic contracts for the market-data foundation harness."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.alerts.telegram_lifecycle import PUBLIC_SIGNAL_MIN_RR
from app.analytics.public_signal_quality import (
    MIN_PUBLIC_SETUP_QUALITY_SCORE,
    MIN_PUBLIC_SIGNAL_GRADE,
)
from app.core.minimum_rr import DEFAULT_CONFIGURED_MINIMUM_RR
from app.data.candle_freshness import POLICY_VERSION, PUBLICATION_ALLOWANCE
from app.data.candle_integrity import validate_candle_sequence
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.owner_monitoring import discovery_failure_continues_owner_monitoring
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import active_lifecycle_symbols
from app.storage.database import SCHEMA_VERSION
from app.universe.symbol_universe import UniverseResolutionError
from research.market_data_foundation.budget import BudgetExceeded, StudyBudget
from research.market_data_foundation.catalog import inventory_rows
from research.market_data_foundation.cmc import (
    CmcAsset,
    CmcContractError,
    CmcListingPage,
    membership_problems,
    parse_global_metrics,
    parse_listings,
)
from research.market_data_foundation.evidence import Observation, sanitize
from research.market_data_foundation.integrity import (
    binance_current_open_interest_unit,
    bybit_open_interest_fields,
    bybit_ticker_timestamp_source,
    canonical_findings,
    closed_bar_status,
    diagnose_sequence,
    funding_fraction_percent,
    liquidation_observation,
    ohlc_problems,
    order_book_snapshot_diagnosis,
    populations_are_same_metric,
    production_integer_timestamp_is_milliseconds,
    resample_2d_checked,
    unsupported_resample,
)
from research.market_data_foundation.online import run_public_sample
from research.market_data_foundation.paths import PathGuardError, resolve_output, resolve_research_root
from research.market_data_foundation.reports import render_package
from research.market_data_foundation.safety import UnsafeProcessConfiguration, enforce_research_process
from research.market_data_foundation.shadow import (
    cache_provenance,
    derivatives_sensitivity,
    freshness_sensitivity,
    pair_exact_opens,
    public_gate_probe,
)
from research.market_data_foundation.timeutil import (
    classify_unit_confusion,
    future_timestamp,
    normalize_explicit,
    originally_available,
)
from research.market_data_foundation.top100 import (
    IdentityCrosswalk,
    OwnerFixture,
    VenueContract,
    assert_stage_arithmetic,
    evaluate_top100,
    select_shadow_sample,
)
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY

pytestmark = pytest.mark.filterwarnings("ignore")


def _asset(rank: int, symbol: str, provider_id: str | None = None) -> CmcAsset:
    return CmcAsset(
        provider_id=provider_id or str(rank),
        symbol=symbol,
        name=symbol,
        rank=rank,
        last_updated="2026-01-01T00:00:00.000Z",
        quote_last_updated="2026-01-01T00:00:00.000Z",
        market_cap_usd="10",
        price_usd="1",
        circulating_supply="10",
        rank_status="valid",
    )


def _page(assets: tuple[CmcAsset, ...]) -> CmcListingPage:
    return CmcListingPage(
        assets=assets,
        envelope_timestamp="2026-01-01T00:00:00.000Z",
        credit_count="1",
        error_code="0",
        error_message=None,
        synthetic=True,
        raw_sha256="synthetic",
    )


_ADMITTED_AT = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)


def _contract(
    base: str,
    volume: str | None = "100",
    *,
    settlement_asset: str = "USDT",
    multiplier: str = "1",
    quote_asset: str = "USDT",
    contract_type: str = "PERPETUAL",
) -> VenueContract:
    return VenueContract(
        venue="binance_usdm",
        symbol=f"{base}USDT",
        base_asset=base,
        quote_asset=quote_asset,
        contract_type=contract_type,
        status="TRADING",
        underlying_type="COIN",
        quote_volume_usdt=volume,
        settlement_asset=settlement_asset,
        multiplier=multiplier,
    )


def _crosswalk(base: str, provider_id: str, *, multiplier: str = "1") -> IdentityCrosswalk:
    return IdentityCrosswalk(
        cmc_id=provider_id,
        venue="binance_usdm",
        base_asset=base,
        quote_asset="USDT",
        settlement_asset="USDT",
        contract_type="PERPETUAL",
        multiplier=multiplier,
        source="synthetic-fixture",
        retrieved_at="2026-01-01T00:05:00+00:00",
        source_record_id=f"fixture:{provider_id}",
    )


def _admit() -> dict[str, datetime]:
    return {"acquired_at": _ADMITTED_AT, "decision_cutoff": _ADMITTED_AT}


def _candle(open_ms: int, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": open_ms,
        "open": Decimal("10"),
        "high": Decimal("12"),
        "low": Decimal("9"),
        "close": Decimal("11"),
        "volume": Decimal("1"),
    }
    row.update(overrides)
    return row


def test_production_gates_and_schema_are_unchanged() -> None:
    assert MIN_PUBLIC_SETUP_QUALITY_SCORE == Decimal("88")
    assert MIN_PUBLIC_SIGNAL_GRADE == "A"
    assert PUBLIC_SIGNAL_MIN_RR == Decimal("3")
    assert DEFAULT_CONFIGURED_MINIMUM_RR == Decimal("2.5")
    assert POLICY_VERSION == "essential-candle-freshness-v1"
    assert PUBLICATION_ALLOWANCE == timedelta(0)
    assert SCHEMA_VERSION == 27


def test_app_package_does_not_reference_the_harness() -> None:
    root = Path("app")
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "market_data_foundation" not in text


def test_inventory_anchors_match_source() -> None:
    for row in inventory_rows():
        assert row["runtime_enabled"] == "unverified" or row["runtime_enabled"].startswith("unverified")
        assert row["expectancy_benefit"] == "not established"
        for ref in row["code_refs"]:
            line = Path(ref["path"]).read_text(encoding="utf-8").splitlines()[ref["line"] - 1]
            assert ref["contains"] in line


def test_missing_observation_cannot_be_zero() -> None:
    with pytest.raises(ValueError, match="zero"):
        Observation(
            evidence_id="e",
            run_id="r",
            code_sha="sha",
            policy_digest="d",
            provider="p",
            endpoint="/",
            request_id="q",
            parameters={},
            venue="v",
            instrument_id="i",
            asset_id="a",
            native_value=None,
            native_unit="contracts",
            normalized_value="0",
            normalized_unit="contracts",
            timestamps={"measurement_at": None},
            interval_semantics="snapshot",
            cache_status="miss",
            lineage="source",
            response_status="200",
            latency_ms=1,
            retry_count=0,
            request_weight=None,
            raw_sha256=None,
            validity="missing",
            reason_code="absent",
        )


def test_secret_parameters_are_redacted() -> None:
    cleaned = sanitize({"X-CMC_PRO_API_KEY": "secret-value", "symbol": "BTC", "note": "Bearer abc"})
    assert cleaned["X-CMC_PRO_API_KEY"] == "[redacted]"
    assert cleaned["symbol"] == "BTC"
    assert "abc" not in cleaned["note"]


def test_paths_reject_escape_and_runtime_database(tmp_path: Path) -> None:
    root = tmp_path / "research"
    root.mkdir()
    with pytest.raises(PathGuardError):
        resolve_output(root, "../outside.json")
    with pytest.raises(PathGuardError):
        resolve_output(root, "main_live_runtime.sqlite")
    runtime = tmp_path / "Candle Craft Inteligence"
    with pytest.raises(PathGuardError):
        resolve_research_root(runtime)


def test_budget_stops_at_the_cap() -> None:
    budget = StudyBudget(max_http_attempts=1, max_cmc_attempts=1)
    budget.ensure_can_attempt("cmc")
    budget.record_attempt("cmc", 10)
    with pytest.raises(BudgetExceeded):
        budget.ensure_can_attempt("cmc")


def test_unsafe_process_flag_aborts() -> None:
    with pytest.raises(UnsafeProcessConfiguration):
        enforce_research_process(
            {
                "TELEGRAM_DRY_RUN": "false",
                "TELEGRAM_SIGNALS_ENABLED": "false",
                "LOCAL_MANUAL_MODE": "true",
                "ORDER_EXECUTION_ENABLED": "false",
            }
        )


def test_timestamp_units_cutoff_and_future() -> None:
    seconds = normalize_explicit(1_700_000_000, "s")
    millis = production_integer_timestamp_is_milliseconds(1_700_000_000_000)
    assert seconds == millis
    assert production_integer_timestamp_is_milliseconds(1_700_000_000).year == 1970
    assert (
        classify_unit_confusion(1_700_000_000, interval=timedelta(minutes=1), sibling_delta=60)
        == "spacing_matches_seconds"
    )
    cutoff = datetime(2026, 1, 2, tzinfo=UTC)
    assert (
        originally_available(
            measurement_at=datetime(2026, 1, 1, tzinfo=UTC),
            available_to_cci_at=datetime(2026, 1, 3, tzinfo=UTC),
            decision_cutoff=cutoff,
        )
        == "incompatible"
    )
    assert originally_available(
        measurement_at=None,
        available_to_cci_at=cutoff,
        decision_cutoff=cutoff,
    ) == "unverified"
    received = datetime(2026, 1, 1, tzinfo=UTC)
    assert future_timestamp(received + timedelta(minutes=1), received_at=received)


def test_candle_semantics_do_not_treat_flat_zero_volume_as_a_gap() -> None:
    present = _candle(0, open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"), volume=Decimal("0"))
    assert "present_no_trade" in ohlc_problems(present)
    assert "continuity_gap" not in diagnose_sequence((present,), timeframe="1m")
    broken = _candle(0, high=Decimal("1"), low=Decimal("5"))
    assert "impossible_ohlc" in ohlc_problems(broken)
    validate_candle_sequence((broken,), timeframe="1m")
    negative = _candle(0, volume=Decimal("-1"))
    assert any(item.startswith("volume_negative") for item in diagnose_sequence((negative,), timeframe="1m"))
    gapped = (_candle(0), _candle(120_000))
    assert "continuity_gap" in diagnose_sequence(gapped, timeframe="1m")
    status = closed_bar_status((_candle(0),), timeframe="1m", decision_at=datetime(1970, 1, 1, 0, 0, 30, tzinfo=UTC))
    assert status["excluded_unclosed_count"] == 1
    assert status["closed_count"] == 0


def test_two_day_resample_conserves_volume_and_rejects_other_targets() -> None:
    produced = resample_2d_checked(
        (
            {"timestamp": 0, "open": "10", "high": "12", "low": "9", "close": "11", "volume": "1.5"},
            {"timestamp": 86_400_000, "open": "11", "high": "13", "low": "10", "close": "12", "volume": "2.5"},
        ),
        decision_at=datetime(1970, 1, 3, tzinfo=UTC),
    )
    assert produced["emitted"] == 1
    assert Decimal(produced["rows"][0]["volume"]) == Decimal("4.0")
    assert produced["rows"][0]["open"] == "10"
    assert produced["rows"][0]["close"] == "12"
    assert resample_2d_checked(({"timestamp": 0, "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"},), decision_at=datetime(1970, 1, 3, tzinfo=UTC))["emitted"] == 0
    assert "2d" in unsupported_resample("12h")


def test_oi_funding_book_and_liquidations() -> None:
    fields = bybit_open_interest_fields(
        {"openInterest": "20", "singleOpenInterest": "10", "timestamp": "1700000000000"}
    )
    assert fields["repository_open_interest"] == "20"
    assert fields["divided_by_two"] is False
    assert fields["disposition"] == "unverified"
    assert binance_current_open_interest_unit(
        {"symbol": "BTCUSDT", "openInterest": "2.5", "time": 1_700_000_000_000}
    ) == "N/A"
    funding = funding_fraction_percent(Decimal("0.0001"))
    assert Decimal(funding["percent"]) == Decimal("0.01")
    assert funding_fraction_percent(Decimal("1"))["disposition"] == "incompatible"
    ticker = bybit_ticker_timestamp_source(1_700_000_000_000)
    assert ticker["copied_envelope_time"] is True
    assert ticker["measurement_timestamp"] is None
    book = order_book_snapshot_diagnosis(
        {
            "lastUpdateId": 7,
            "E": 1_700_000_000_000,
            "T": 1_700_000_000_000,
            "bids": [["100", "1"]],
            "asks": [["99", "1"]],
        }
    )
    assert book["crossed"] is True
    assert book["synchronization"] == "not_applicable"
    empty = liquidation_observation(None, coverage="no_events_received")
    assert empty["disposition"] == "missing"
    assert empty["event_count"] is None
    observed = liquidation_observation(
        {
            "e": "forceOrder",
            "st": 1,
            "ps": "BTCUSDT",
            "E": 1_700_000_000_000,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "q": "1",
                "p": "100",
                "ap": "100",
                "X": "FILLED",
                "l": "1",
                "z": "1",
                "T": 1_700_000_000_000,
            },
        },
        coverage="observed_event",
    )
    assert observed["side"] == "LONG"
    assert populations_are_same_metric(
        "binance_global_long_short_account_ratio",
        "top_trader_position_ratio",
    ) is False


def test_cmc_envelope_and_rank_boundary() -> None:
    with pytest.raises(CmcContractError, match="api_error"):
        parse_listings(
            {"status": {"error_code": 400, "error_message": "bad"}, "data": []},
            raw_sha256="synthetic",
            synthetic=True,
        )
    page = parse_listings(_synthetic_payload(), raw_sha256="synthetic", synthetic=True)
    assert page.credit_count == "1"
    assert membership_problems(page) == ()
    broken = _synthetic_payload()
    broken["data"][0]["cmc_rank"] = None
    parsed = parse_listings(broken, raw_sha256="synthetic", synthetic=True)
    assert "null_or_invalid_rank" in membership_problems(parsed)
    assets = tuple(_asset(rank, "USDT" if rank == 50 else f"C{rank}") for rank in range(1, 102))
    counts = evaluate_top100(
        _page(assets),
        tuple(_contract(f"C{rank}") for rank in range(1, 101) if rank != 50),
        owners=(OwnerFixture("OWNEDUSDT", "CONFIRMED"),),
        count_kind="synthetic",
        **_admit(),
    )
    assert counts.membership_status == "unverified"
    assert "101" not in {row["rank"] for row in counts.exclusions if row["reason"] == "existing_stable_or_leveraged_policy"}
    assert all(symbol != "C101USDT" for symbol in counts.queued_symbols)
    assert counts.owner_monitoring_count == 1
    assert counts.owners_outside_discovery == 1
    assert counts.scan_attempts == 0
    assert_stage_arithmetic(counts)
    exact = tuple(_asset(rank, "USDT" if rank == 50 else ("WBTC" if rank == 10 else f"C{rank}")) for rank in range(1, 101))
    exact_counts = evaluate_top100(
        _page(exact),
        tuple(_contract(f"C{rank}") for rank in range(1, 101) if rank not in {10, 50})
        + (_contract("WBTC", "5"),),
        crosswalk=tuple(_crosswalk(asset.symbol, asset.provider_id) for asset in exact),
        count_kind="synthetic",
        **_admit(),
    )
    assert exact_counts.membership_status == "verified"
    assert "WBTCUSDT" in exact_counts.queued_symbols
    assert "BTCUSDT" not in exact_counts.queued_symbols
    assert "USDTUSDT" not in exact_counts.queued_symbols
    collided = (
        _asset(1, "FOO", "1"),
        _asset(2, "FOO", "2"),
        *tuple(_asset(rank, f"C{rank}") for rank in range(3, 101)),
    )
    collision = evaluate_top100(
        _page(collided),
        (_contract("FOO"),) + tuple(_contract(f"C{rank}") for rank in range(3, 101)),
        crosswalk=tuple(_crosswalk(asset.symbol, asset.provider_id) for asset in collided),
        count_kind="synthetic",
        **_admit(),
    )
    assert "FOOUSDT" not in collision.queued_symbols
    volume_assets = tuple(_asset(rank, f"C{rank}") for rank in range(1, 101))
    missing_volume = evaluate_top100(
        _page(volume_assets),
        tuple(_contract(f"C{rank}", None) for rank in range(1, 101)),
        crosswalk=tuple(_crosswalk(asset.symbol, asset.provider_id) for asset in volume_assets),
        count_kind="synthetic",
        **_admit(),
    )
    assert missing_volume.unique_queued_discovery_contracts == 0
    assert missing_volume.insufficient_evidence_rejections == 100


def test_multiplier_symbol_is_not_stripped_or_merged() -> None:
    assets = tuple(_asset(rank, "PEPE" if rank == 1 else f"C{rank}") for rank in range(1, 101))
    counts = evaluate_top100(
        _page(assets),
        (_contract("1000PEPE"),) + tuple(_contract(f"C{rank}") for rank in range(2, 101)),
        crosswalk=tuple(_crosswalk(asset.symbol, asset.provider_id) for asset in assets),
        count_kind="synthetic",
        **_admit(),
    )
    assert "1000PEPEUSDT" not in counts.queued_symbols
    assert "PEPEUSDT" not in counts.queued_symbols


def test_shadow_sample_is_deterministic_and_keeps_unmatched_opens() -> None:
    queued = [(f"S{index}USDT", str(1000 - index)) for index in range(10)]
    queued.extend([("BTCUSDT", "5000"), ("ETHUSDT", "4000")])
    first = select_shadow_sample(queued)
    second = select_shadow_sample(list(reversed(queued)))
    assert first == second
    assert first["selected"][0] == "BTCUSDT"
    assert first["selected"][1] == "ETHUSDT"
    assert len(first["selected"]) == 8
    paired = pair_exact_opens(
        ({"timestamp": 1, "close": "10"}, {"timestamp": 2, "close": "11"}),
        ({"timestamp": 2, "close": "12"}, {"timestamp": 3, "close": "13"}),
    )
    assert paired["unmatched_left"] == [1]
    assert paired["unmatched_right"] == [3]
    assert paired["paired"][0]["close_delta"] == "-1"


def test_pure_consumer_sensitivity_and_cache(tmp_path: Path) -> None:
    gates = public_gate_probe()
    assert gates["score_88_grade_A"] is True
    assert gates["score_87_grade_A"] is False
    assert gates["score_90_grade_B"] is False
    assert gates["unevaluated"] is False
    freshness = freshness_sensitivity()
    assert freshness["covered"] == "pass"
    assert freshness["missing_latest_bar"] == "stale"
    derivatives = derivatives_sensitivity()
    assert derivatives["price_only_position_growth"] == "false"
    assert derivatives["missing_previous_oi"] == "N/A"
    cache = cache_provenance(tmp_path)
    assert cache["miss_kind"] == "miss"
    assert cache["hit_kind"] == "hit"
    assert cache["fetch_calls_before_reload"] == 1
    assert cache["failed_fetch_raised"] is True
    assert cache["failed_venue_stored"] is False
    assert cache["reload_file_loaded"] is True
    assert cache["measurement_at"] != str(cache["cache_created_at"])


def test_owner_monitoring_is_independent_of_discovery(tmp_path: Path) -> None:
    database = tmp_path / "owners.sqlite"
    record = SetupLifecycleRecord(
        lifecycle_id="life-owned",
        symbol="OWNEDUSDT",
        current_state=SetupLifecycleState.CONFIRMED,
        first_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_at="2026-01-01T00:00:00+00:00",
        last_transition_at="2026-01-01T00:00:00+00:00",
        confirmed_at="2026-01-01T00:00:00+00:00",
    )
    with SQLiteSetupLifecycleRepository(database, expected_identity=SYNTHETIC_IDENTITY) as repository:
        repository.upsert_record(record)
    assert active_lifecycle_symbols((), database_path=database, expected_identity=SYNTHETIC_IDENTITY) == (
        "OWNEDUSDT",
    )
    assert discovery_failure_continues_owner_monitoring(UniverseResolutionError("universe_error: down")) is True


def test_reports_name_every_required_section() -> None:
    package = render_package()
    for name in (
        "A_inventory.md",
        "B_dependency_map.md",
        "C_candle_volume.md",
        "D_oi_funding_positioning.md",
        "E_freshness_missingness.md",
        "F_provider_matrix.md",
        "G_cmc_mcp.md",
        "H_top100.md",
        "I_shadow.md",
        "J_cadence_cost.md",
        "K_recommendations.md",
        "L_review_packet.md",
    ):
        assert name in package
        assert package[name].strip()
    assert "not established" in package["A_inventory.md"]
    assert any(item.finding_id == "MDF-LIQ-001" for item in canonical_findings())
    root = Path("docs/research/market_data_foundation")
    for name, text in package.items():
        assert (root / name).read_text(encoding="utf-8") == text


def test_synthetic_transport_sample_is_not_labeled_live(tmp_path: Path) -> None:
    payload = _synthetic_payload()
    body = json.dumps(payload).encode("utf-8")
    global_body = json.dumps(
        {
            "status": {"error_code": 0, "timestamp": "2026-01-01T00:00:00.000Z", "credit_count": 1},
            "data": {
                "btc_dominance": "50",
                "eth_dominance": "10",
                "last_updated": "2026-01-01T00:00:00.000Z",
                "quote": {"USD": {"total_market_cap": "1000"}},
            },
        }
    ).encode("utf-8")
    exchange = json.dumps(
        {
            "symbols": [
                {
                    "symbol": "C2USDT",
                    "baseAsset": "C2",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "underlyingType": "COIN",
                }
            ]
        }
    ).encode("utf-8")
    tickers = json.dumps([{"symbol": "C2USDT", "quoteVolume": "10"}]).encode("utf-8")
    routes = {
        "https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/listings/latest": body,
        "https://pro-api.coinmarketcap.com/public-api/v1/global-metrics/quotes/latest": global_body,
        "https://fapi.binance.com/fapi/v1/exchangeInfo": exchange,
        "https://fapi.binance.com/fapi/v1/ticker/24hr": tickers,
        "https://api.coinpaprika.com/v1/tickers": b"[]",
    }
    summary = run_public_sample(
        root=tmp_path,
        client=SimpleNamespace(stream=lambda method, url, params=None: _Response(routes[url])),
        budget=StudyBudget(max_http_attempts=20),
        transport_label="synthetic_transport",
        clock=lambda: _ADMITTED_AT,
    )
    assert summary["transport_label"] == "synthetic_transport"
    assert summary["top100"]["structural_status"] == "verified"
    assert summary["top100"]["temporal_status"] == "verified"
    assert summary["top100"]["identity_validated_assets"] == 0
    assert summary["top100"]["unique_queued_discovery_contracts"] == 0
    assert summary["top100"]["scan_attempts"] == 0
    assert summary["raw_bodies_retained"] is False
    assert (tmp_path / "observations.json").exists()
    assert summary["coinglass"] == "not_called_no_authorized_no_overage_entitlement"
    assert summary["coinpaprika_overlap"]["validated_identity_overlap"] == 0
    assert summary["coinpaprika_overlap"]["identity_basis"] == "symbol_only_unverified"


class _Response:
    def __init__(self, body: bytes, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def iter_bytes(self):
        yield self._body


def _synthetic_payload() -> dict[str, object]:
    rows = []
    for rank in range(1, 101):
        symbol = "BTC" if rank == 1 else f"C{rank}"
        rows.append(
            {
                "id": rank,
                "name": symbol,
                "symbol": symbol,
                "cmc_rank": rank,
                "last_updated": "2026-01-01T00:00:00.000Z",
                "circulating_supply": "1",
                "quote": {"USD": {"price": "1", "market_cap": "10", "last_updated": "2026-01-01T00:00:00.000Z"}},
            }
        )
    return {
        "status": {
            "error_code": 0,
            "error_message": None,
            "timestamp": "2026-01-01T00:00:00.000Z",
            "credit_count": 1,
        },
        "data": rows,
        "synthetic": True,
    }


def test_r1_ticker_match_without_crosswalk_is_not_validated() -> None:
    payload = _synthetic_payload()
    payload["data"][0]["id"] = 999999
    payload["data"][0]["name"] = "Unrelated same-ticker asset"
    page = parse_listings(payload, raw_sha256="synthetic", synthetic=True)
    unrelated = evaluate_top100(page, (_contract("BTC"),), count_kind="synthetic", **_admit())
    assert unrelated.identity_validated_assets == 0
    assert "BTCUSDT" not in unrelated.queued_symbols
    mapped = evaluate_top100(
        page,
        (_contract("BTC"),),
        crosswalk=(_crosswalk("BTC", "999999"),),
        count_kind="synthetic",
        **_admit(),
    )
    assert "BTCUSDT" in mapped.queued_symbols
    settled = evaluate_top100(
        _page(tuple(_asset(rank, "BTC" if rank == 1 else f"C{rank}") for rank in range(1, 101))),
        (_contract("BTC", settlement_asset="USD"),)
        + tuple(_contract(f"C{rank}") for rank in range(2, 101)),
        crosswalk=tuple(_crosswalk("BTC" if rank == 1 else f"C{rank}", str(rank)) for rank in range(1, 101)),
        count_kind="synthetic",
        **_admit(),
    )
    assert "BTCUSDT" not in settled.queued_symbols


def test_r2_invalid_time_and_incomplete_rank_do_not_queue() -> None:
    payload = _synthetic_payload()
    payload["status"]["timestamp"] = "not-a-timestamp"
    for row in payload["data"]:
        row["last_updated"] = "not-a-timestamp"
        row["quote"]["USD"]["last_updated"] = None
    page = parse_listings(payload, raw_sha256="synthetic", synthetic=True)
    assert membership_problems(page) == ()
    from research.market_data_foundation.cmc import temporal_problems

    codes = temporal_problems(page, acquired_at=_ADMITTED_AT, decision_cutoff=_ADMITTED_AT)
    assert "malformed_envelope_timestamp" in codes
    assert "malformed_asset_timestamp" in codes
    assert "missing_quote_timestamp" in codes
    blocked = evaluate_top100(
        page,
        (_contract("BTC"),),
        crosswalk=(_crosswalk("BTC", "1"),),
        count_kind="synthetic",
        **_admit(),
    )
    assert blocked.structural_status == "verified"
    assert blocked.temporal_status == "unverified"
    assert blocked.unique_queued_discovery_contracts == 0
    short = parse_listings(
        {**_synthetic_payload(), "data": _synthetic_payload()["data"][:1]},
        raw_sha256="synthetic",
        synthetic=True,
    )
    incomplete = evaluate_top100(short, (_contract("BTC"),), count_kind="synthetic", **_admit())
    assert incomplete.membership_status == "unverified"
    assert incomplete.queued_symbols == ()
    metrics = parse_global_metrics(
        {
            "status": {"error_code": 0, "timestamp": "bad"},
            "data": {
                "btc_dominance": -1,
                "eth_dominance": 200,
                "last_updated": "bad",
                "quote": {"USD": {"total_market_cap": -5}},
            },
        },
        raw_sha256="synthetic",
        synthetic=True,
    )
    assert "impossible_btc_dominance" in metrics.domain_problems
    assert "impossible_eth_dominance" in metrics.domain_problems
    assert "impossible_total_market_cap_usd" in metrics.domain_problems
    assert "malformed_last_updated" in metrics.domain_problems
    future = _synthetic_payload()
    future["data"][0]["last_updated"] = "2030-01-01T00:00:00+00:00"
    future_page = parse_listings(future, raw_sha256="synthetic", synthetic=True)
    future_counts = evaluate_top100(future_page, (_contract("BTC"),), count_kind="synthetic", **_admit())
    assert "future_asset_timestamp" in future_counts.temporal_problem_codes
    assert future_counts.queued_symbols == ()


def test_r3_provider_failures_stay_isolated(tmp_path: Path) -> None:
    def client_for(first: _Response) -> SimpleNamespace:
        calls = {"n": 0}

        def stream(method, url, params=None):
            calls["n"] += 1
            calls.setdefault("urls", []).append(url)
            if calls["n"] == 1:
                return first
            if "global-metrics" in url:
                return _Response(
                    json.dumps(
                        {
                            "status": {"error_code": 0, "timestamp": "2026-01-01T00:00:00Z"},
                            "data": {
                                "btc_dominance": 50,
                                "eth_dominance": 10,
                                "last_updated": "2026-01-01T00:00:00Z",
                                "quote": {"USD": {"total_market_cap": 100}},
                            },
                        }
                    ).encode()
                )
            if "exchangeInfo" in url:
                return _Response(b'{"symbols":[]}')
            if "ticker/24hr" in url:
                return _Response(b"[]")
            if "coinpaprika" in url:
                return _Response(b"[]")
            return _Response(b"{}")

        stream.calls = calls
        return SimpleNamespace(stream=stream)

    api_error = client_for(
        _Response(
            json.dumps(
                {"status": {"error_code": 1008, "error_message": "plan unavailable"}, "data": []}
            ).encode()
        )
    )
    summary = run_public_sample(
        root=tmp_path / "api",
        client=api_error,
        budget=StudyBudget(max_http_attempts=20),
        transport_label="synthetic_transport",
        clock=lambda: _ADMITTED_AT,
        sleeper=lambda _delay: None,
    )
    assert (tmp_path / "api" / "sample_summary.json").exists()
    assert any(item["provider"] == "binance" for item in summary["calls"])
    forbidden = client_for(_Response(b'{"error":"forbidden"}', status_code=403))
    blocked = run_public_sample(
        root=tmp_path / "forbidden",
        client=forbidden,
        budget=StudyBudget(max_http_attempts=20),
        transport_label="synthetic_transport",
        clock=lambda: _ADMITTED_AT,
        sleeper=lambda _delay: None,
    )
    assert any("fapi.binance.com" in url for url in forbidden.stream.calls["urls"])
    assert blocked["partial_reason"] != "cmc_stopped"
    from research.market_data_foundation.online import _btc_cross_venue

    compared = _btc_cross_venue(
        {"status_code": 400, "disposition": "malformed", "json": {"code": -1000}},
        {"status_code": 200, "disposition": "verified", "json": {"retCode": 10006, "result": {}}},
    )
    assert compared["status"] == "not_observed"
    assert compared["comparable_observations"] == 0


def test_r3_rate_limit_empty_and_malformed_bodies_still_save_a_summary(tmp_path: Path) -> None:
    def run(first: _Response, name: str) -> dict[str, object]:
        urls: list[str] = []

        def stream(method, url, params=None):
            urls.append(url)
            if len(urls) == 1:
                return first
            if "exchangeInfo" in url:
                return _Response(b'{"symbols":[]}')
            if "ticker/24hr" in url or "coinpaprika" in url:
                return _Response(b"[]")
            return _Response(b'{"status":{"error_code":0,"timestamp":"2026-01-01T00:00:00Z"},"data":{}}')

        summary = run_public_sample(
            root=tmp_path / name,
            client=SimpleNamespace(stream=stream),
            budget=StudyBudget(max_http_attempts=20, max_retries=0),
            transport_label="synthetic_transport",
            clock=lambda: _ADMITTED_AT,
            sleeper=lambda _delay: None,
        )
        assert (tmp_path / name / "sample_summary.json").exists()
        assert any("fapi.binance.com" in url for url in urls)
        return summary

    limited = _Response(b'{"status":{"error_code":0,"error_message":"rate"}}', status_code=429)
    limited.headers["Retry-After"] = "1"
    rate = run(limited, "rate")
    assert rate["calls"][0]["status_code"] == 429
    empty = run(_Response(b""), "empty")
    assert empty["cmc_listings"] is None
    malformed = run(_Response(b"{"), "malformed")
    assert malformed["calls"][0]["error"] is None
    assert malformed["cmc_listings"] is None
    parser = run(
        _Response(
            json.dumps(
                {"status": {"error_code": 0, "timestamp": "2026-01-01T00:00:00Z"}, "data": {"not": "a list"}}
            ).encode()
        ),
        "parser",
    )
    assert any(item["reason"] == "listings_data_not_a_list" for item in parser["provider_errors"])


def test_r4_replay_uses_saved_observations_and_rejects_late_acquisition(tmp_path: Path) -> None:
    from research.market_data_foundation.online import replay_saved

    run_public_sample(
        root=tmp_path,
        client=SimpleNamespace(
            stream=lambda method, url, params=None: _Response(
                json.dumps(_synthetic_payload()).encode()
                if "listings" in url
                else b'{"status":{"error_code":0,"timestamp":"2026-01-01T00:00:00Z"},"data":{"btc_dominance":"50","eth_dominance":"10","last_updated":"2026-01-01T00:00:00Z","quote":{"USD":{"total_market_cap":"1"}}}}'
                if "global-metrics" in url
                else b'{"symbols":[]}'
                if "exchangeInfo" in url
                else b"[]"
            )
        ),
        budget=StudyBudget(max_http_attempts=20),
        transport_label="synthetic_transport",
        clock=lambda: _ADMITTED_AT,
        code_sha="abc",
        sleeper=lambda _delay: None,
    )
    names = {path.name for path in tmp_path.iterdir()}
    assert "observations.json" in names
    assert "sample_summary.json" in names
    saved = json.loads((tmp_path / "observations.json").read_text(encoding="utf-8"))
    assert saved["raw_bodies_retained"] is False
    assert saved["checksum"]
    late = replay_saved(tmp_path, decision_cutoff=datetime(2026, 1, 1, tzinfo=UTC))
    assert late["status"] == "replayed"
    assert late["admitted"] == []
    assert late["inadmissible"]
    admitted = replay_saved(tmp_path, decision_cutoff=datetime(2026, 1, 1, 0, 6, tzinfo=UTC))
    assert admitted["admitted"]
    assert all(
        item["availability"] == "verified" for item in admitted["admitted"]
    )


def test_r5_stream_deadline_and_retry_after_http_date(monkeypatch, tmp_path: Path) -> None:
    from research.market_data_foundation.online import _get, _retry_delay

    clock = {"t": 0.0}

    class Slow(_Response):
        def iter_bytes(self):
            clock["t"] = 20.0
            yield b"{}"

    monkeypatch.setattr("research.market_data_foundation.budget.time.monotonic", lambda: clock["t"])
    budget = StudyBudget(deadline_monotonic=10.0)
    calls: list[dict[str, object]] = []
    result = _get(
        SimpleNamespace(stream=lambda method, url, params=None: Slow(b"{}")),
        budget,
        calls,
        provider="cmc",
        url="https://synthetic.invalid",
        clock=lambda: _ADMITTED_AT,
        sleeper=lambda _delay: None,
        run_id="r",
        code_sha="s",
        policy_digest="p",
    )
    assert result["disposition"] != "verified"
    assert calls[0]["stopped_reason"] == "deadline_during_stream"
    far = _retry_delay("Thu, 01 Jan 2099 00:00:00 GMT", now=datetime(2026, 10, 19, tzinfo=UTC))
    assert far == (datetime(2099, 1, 1, tzinfo=UTC) - datetime(2026, 10, 19, tzinfo=UTC)).total_seconds()
    assert far > 30
    assert _retry_delay("120", now=_ADMITTED_AT) == 120.0
    assert _retry_delay(None, now=_ADMITTED_AT) == 1.0
    skipped = run_public_sample(
        root=tmp_path,
        client=SimpleNamespace(stream=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("live call"))),
        budget=StudyBudget(),
        transport_label="live",
    )
    assert skipped["partial_reason"] == "live_sampling_skipped_unknown_shared_ip_capacity"
    assert skipped["calls"] == []


def test_r6_futures_book_uses_previous_update_id() -> None:
    from app.microstructure.order_book import (
        BinanceDepthEvent,
        BinanceDepthSnapshot,
        BookIngestOutcome,
        DepthLevelUpdate,
        SynchronizedLocalOrderBook,
    )

    moment = datetime(2026, 10, 18, 12, tzinfo=UTC)
    book = SynchronizedLocalOrderBook("BTCUSDT", stale_after_seconds=5, event_buffer_size=8)
    book.mark_connected(reconnect=False)
    bridge = BinanceDepthEvent(
        "BTCUSDT",
        "BTCUSDT",
        moment,
        moment,
        10,
        12,
        9,
        (DepthLevelUpdate(Decimal("100"), Decimal("2")), DepthLevelUpdate(Decimal("99"), Decimal("1"))),
        (DepthLevelUpdate(Decimal("101"), Decimal("1")),),
    )
    assert book.ingest(bridge, received_at=moment) == BookIngestOutcome.BUFFERED
    synced = book.install_snapshot(
        BinanceDepthSnapshot(
            10,
            moment,
            moment,
            (DepthLevelUpdate(Decimal("100"), Decimal("1")), DepthLevelUpdate(Decimal("99"), Decimal("1"))),
            (DepthLevelUpdate(Decimal("101"), Decimal("1")),),
        ),
        received_at=moment,
    )
    assert synced == BookIngestOutcome.SYNCHRONIZED
    broken = BinanceDepthEvent("BTCUSDT", "BTCUSDT", moment, moment, 11, 12, 9, (), ())
    resync = SynchronizedLocalOrderBook("BTCUSDT", stale_after_seconds=5, event_buffer_size=8)
    resync.last_update_id = 10
    resync.synchronized = True
    resync.bids[Decimal("100")] = Decimal("1")
    resync.asks[Decimal("101")] = Decimal("1")
    assert resync.ingest(broken, received_at=moment) == BookIngestOutcome.NEEDS_RESYNC
    book.mark_connected(reconnect=True)
    assert book.synchronized is False
    fresh = SynchronizedLocalOrderBook("BTCUSDT", stale_after_seconds=5, event_buffer_size=8)
    fresh.last_update_id = 12
    fresh.synchronized = True
    fresh.bids[Decimal("100")] = Decimal("2")
    fresh.bids[Decimal("99")] = Decimal("1")
    fresh.asks[Decimal("101")] = Decimal("1")
    deleted = BinanceDepthEvent(
        "BTCUSDT",
        "BTCUSDT",
        moment,
        moment,
        13,
        13,
        12,
        (DepthLevelUpdate(Decimal("100"), Decimal("0")),),
        (),
    )
    assert fresh.ingest(deleted, received_at=moment) == BookIngestOutcome.APPLIED
    assert Decimal("100") not in fresh.bids
    duplicate = BinanceDepthEvent("BTCUSDT", "BTCUSDT", moment, moment, 13, 13, 12, (), ())
    assert fresh.ingest(duplicate, received_at=moment) == BookIngestOutcome.IGNORED_OLD


def test_r7_derivatives_and_liquidation_consumers_are_the_production_objects() -> None:
    from research.market_data_foundation.consumers import (
        derivatives_timestamp_probe,
        production_liquidation_coverage_probe,
    )

    derivatives = derivatives_timestamp_probe()
    assert derivatives["source_timestamps_on_enrichment_input"] is False
    assert Decimal(derivatives["previous_open_interest"]) == Decimal("9")
    assert derivatives["rejection_without_timestamp_check"]
    assert derivatives["gate_change"] == "none"
    coverage = production_liquidation_coverage_probe()
    assert coverage["helper_liquidation_observation_is_not_this_consumer"] is True
    assert coverage["disconnected_status"] == "UNAVAILABLE"
    assert coverage["gapped_disconnect_count"] == 1
    assert coverage["covered_empty_is_observed_zero"] is True
    assert coverage["connected_observed_at_is_snapshot_clock"] is True


def test_r8_offline_comparison_and_configured_workload_are_not_live() -> None:
    from research.market_data_foundation.study import offline_metric_comparison, selected_universe_benchmark

    comparison = offline_metric_comparison()
    assert comparison["label"] == "synthetic"
    assert comparison["live"] == "not_executed"
    assert {"close", "base_volume", "open_interest", "funding_fraction"} <= set(comparison["metrics"])
    assert len(comparison["rows"]) == 8
    benchmark = selected_universe_benchmark(tuple(comparison["selected"]))
    assert benchmark["label"] == "synthetic_workload"
    assert benchmark["live_network"] == "not_executed"
    assert benchmark["scanner_invoked"] is False
    assert benchmark["configured_requests"] > 8
    assert benchmark["five_minute_headroom"] == "not_measured"


def test_b1_invalid_quote_domain_and_late_receipt_are_not_masked_by_a_crosswalk() -> None:
    payload = _synthetic_payload()
    payload["data"][0]["quote"]["USD"]["market_cap"] = "-10"
    payload["data"][0]["quote"]["USD"]["price"] = "-1"
    page = parse_listings(payload, raw_sha256="synthetic", synthetic=True)
    invalid = evaluate_top100(
        page,
        (_contract("BTC"),),
        crosswalk=(_crosswalk("BTC", "1"),),
        count_kind="synthetic",
        **_admit(),
    )
    assert invalid.structural_status == "verified"
    assert invalid.temporal_status == "verified"
    assert invalid.domain_status == "unverified"
    assert "impossible_price_usd" in invalid.domain_problems
    assert "impossible_market_cap_usd" in invalid.domain_problems
    assert invalid.membership_status == "unverified"
    assert invalid.queued_symbols == ()
    assert invalid.identity_validated_assets == 0
    restored = parse_listings(_synthetic_payload(), raw_sha256="synthetic", synthetic=True)
    late = evaluate_top100(
        restored,
        (_contract("BTC"),),
        crosswalk=(_crosswalk("BTC", "1"),),
        count_kind="synthetic",
        acquired_at=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        decision_cutoff=datetime(2025, 12, 31, 23, 59, tzinfo=UTC),
    )
    assert late.structural_status == "verified"
    assert late.domain_status == "verified"
    assert "acquisition_after_decision" in late.temporal_problem_codes
    assert "asset_after_decision" in late.temporal_problem_codes
    assert late.membership_status == "unverified"
    assert late.queued_symbols == ()


def _persisted_observations(root: Path) -> list[dict[str, object]]:
    return json.loads((root / "observations.json").read_text(encoding="utf-8"))["observations"]


def test_b2_failure_status_is_the_persisted_observation(tmp_path: Path) -> None:
    def run(first: _Response, name: str) -> tuple[dict[str, object], list[dict[str, object]]]:
        def stream(method, url, params=None):
            if "listings" in url:
                return first
            if "global-metrics" in url:
                return _Response(
                    json.dumps(
                        {
                            "status": {"error_code": 0, "timestamp": "2026-01-01T00:00:00Z"},
                            "data": {
                                "btc_dominance": 50,
                                "eth_dominance": 10,
                                "last_updated": "2026-01-01T00:00:00Z",
                                "quote": {"USD": {"total_market_cap": 100}},
                            },
                        }
                    ).encode()
                )
            if "exchangeInfo" in url:
                return _Response(b'{"symbols":[]}')
            return _Response(b"[]")

        summary = run_public_sample(
            root=tmp_path / name,
            client=SimpleNamespace(stream=stream),
            budget=StudyBudget(max_http_attempts=20, max_retries=0),
            transport_label="synthetic_transport",
            clock=lambda: _ADMITTED_AT,
            sleeper=lambda _delay: None,
        )
        assert (tmp_path / name / "sample_summary.json").exists()
        return summary, _persisted_observations(tmp_path / name)

    malformed_summary, malformed_rows = run(_Response(b"{"), "json")
    first = malformed_rows[0]
    assert first["validity"] != "verified"
    assert malformed_summary["cmc_listings"] is None
    assert any("fapi.binance.com" in call["url"] for call in malformed_summary["calls"])
    encoded_summary, encoded_rows = run(_Response(b"\xff"), "encoding")
    assert encoded_rows[0]["validity"] == "malformed"
    assert encoded_rows[0]["reason_code"] == "invalid_encoding"
    assert encoded_summary["partial_reason"] is None
    empty_rows = run(_Response(b""), "empty")[1]
    assert empty_rows[0]["validity"] == "malformed"
    assert empty_rows[0]["reason_code"] == "empty_body"
    parser_rows = run(
        _Response(
            json.dumps(
                {"status": {"error_code": 0, "timestamp": "2026-01-01T00:00:00Z"}, "data": {"not": "a list"}}
            ).encode()
        ),
        "parser",
    )[1]
    assert parser_rows[0]["validity"] == "malformed"
    assert parser_rows[0]["reason_code"] == "listings_data_not_a_list"


def test_b3_replay_reproduces_normalized_metrics_and_rejects_failures(tmp_path: Path) -> None:
    from research.market_data_foundation.online import replay_saved

    def sample(body: bytes, name: str) -> None:
        def stream(method, url, params=None):
            if "global-metrics" in url:
                return _Response(body)
            if "listings" in url:
                return _Response(json.dumps(_synthetic_payload()).encode())
            if "exchangeInfo" in url:
                return _Response(b'{"symbols":[]}')
            return _Response(b"[]")

        run_public_sample(
            root=tmp_path / name,
            client=SimpleNamespace(stream=stream),
            budget=StudyBudget(max_http_attempts=20, max_retries=0),
            transport_label="synthetic_transport",
            clock=lambda: _ADMITTED_AT,
            code_sha="abc123",
            sleeper=lambda _delay: None,
        )

    sample(
        json.dumps(
            {
                "status": {"error_code": 1008, "error_message": "plan", "timestamp": "2026-01-01T00:00:00Z"},
                "data": {"last_updated": "2026-01-01T00:00:00Z"},
            }
        ).encode(),
        "api-error",
    )
    failed = replay_saved(tmp_path / "api-error", decision_cutoff=datetime(2026, 1, 1, 0, 6, tzinfo=UTC))
    assert failed["raw_bodies_retained"] is False
    assert all(row.get("normalized_unit") != "cmc_global_metrics" or row["validity"] != "verified" for row in failed["admitted"])
    assert failed["reproduced"] == []
    saved_failed = _persisted_observations(tmp_path / "api-error")
    global_failed = next(row for row in saved_failed if "global-metrics" in str(row["endpoint"]))
    assert global_failed["validity"] == "malformed"

    sample(
        json.dumps(
            {
                "status": {"error_code": 0, "timestamp": "2026-01-01T00:00:00Z"},
                "data": {
                    "btc_dominance": 50,
                    "eth_dominance": 10,
                    "last_updated": "2026-01-01T00:00:00Z",
                    "quote": {"USD": {"total_market_cap": 100}},
                },
            }
        ).encode(),
        "metrics",
    )
    cutoff = datetime(2026, 1, 1, 0, 6, tzinfo=UTC)
    replayed = replay_saved(tmp_path / "metrics", decision_cutoff=cutoff)
    assert replayed["reproduced"] == [
        {"btc_dominance": "50", "eth_dominance": "10", "total_market_cap_usd": "100"}
    ]
    assert replayed["admitted"]
    assert all(row["validity"] == "verified" for row in replayed["admitted"])
    late = replay_saved(tmp_path / "metrics", decision_cutoff=datetime(2026, 1, 1, tzinfo=UTC))
    assert late["admitted"] == []
    assert late["reproduced"] == []
    artifact_path = tmp_path / "metrics" / "observations.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["observations"][0]["validity"] = "verified"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    tampered = replay_saved(tmp_path / "metrics", decision_cutoff=cutoff)
    assert tampered["status"] == "malformed"
    assert tampered["reason"] == "checksum_mismatch"
    artifact["schema_version"] = None
    artifact["checksum"] = "ignored"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    invalid = replay_saved(tmp_path / "metrics", decision_cutoff=cutoff)
    assert invalid["status"] == "malformed"
    assert invalid["reason"] == "invalid_schema"


def test_b4_study_bytes_and_retry_after_are_not_shortened(tmp_path: Path) -> None:
    from research.market_data_foundation.online import _get, _retry_delay

    class Chunked:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, method, url, params=None):
            self.calls += 1
            response = _Response(b"")
            response._parts = (b"12345678", b"12345678")

            def iter_bytes():
                yield from response._parts

            response.iter_bytes = iter_bytes
            return response

    client = Chunked()
    summary = run_public_sample(
        root=tmp_path / "bytes",
        client=client,
        budget=StudyBudget(max_total_bytes=10, max_response_bytes=100, max_retries=0, max_http_attempts=20),
        transport_label="synthetic_transport",
        clock=lambda: _ADMITTED_AT,
        sleeper=lambda _delay: None,
    )
    assert client.calls == 1
    assert summary["budget"]["bytes_read"] == 16
    assert summary["partial_reason"] in {"total_bytes_during_stream", "total_bytes"}
    assert summary["budget"]["stop_events"]
    assert summary["budget"]["stop_events"][0]["attempt"] == 0
    assert (tmp_path / "bytes" / "sample_summary.json").exists()
    assert _retry_delay("120", now=_ADMITTED_AT) == 120.0
    dated = _retry_delay("Thu, 01 Jan 2026 00:07:00 GMT", now=datetime(2026, 1, 1, 0, 5, tzinfo=UTC))
    assert dated == 120.0
    slept: list[float] = []
    budget = StudyBudget(max_retries=1, deadline_monotonic=time.monotonic() + 5, max_http_attempts=5)
    calls: list[dict[str, object]] = []
    limited = _Response(b"{}", status_code=429)
    limited.headers["Retry-After"] = "120"
    _get(
        SimpleNamespace(stream=lambda method, url, params=None: limited),
        budget,
        calls,
        provider="cmc",
        url="https://synthetic.invalid",
        clock=lambda: _ADMITTED_AT,
        sleeper=slept.append,
        run_id="r",
        code_sha="real-sha",
        policy_digest="p",
    )
    assert slept == []
    assert calls[0]["stopped_reason"] == "retry_after_exceeds_deadline"
    assert calls[0]["code_sha"] == "real-sha"


def test_b3_replay_cli_uses_real_provenance_and_an_explicit_cutoff(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "market_data_foundation_cli",
        Path("scripts/market_data_foundation.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit, match="decision-cutoff"):
        module.main(["replay", "--research-root", str(tmp_path)])
    sha = module._code_sha(None)
    assert sha != "cli"
    assert len(sha) == 40

