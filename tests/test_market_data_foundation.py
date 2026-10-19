"""Deterministic contracts for the market-data foundation harness."""

from __future__ import annotations

import json
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
    depth_update_gap,
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


def _contract(base: str, volume: str | None = "100") -> VenueContract:
    return VenueContract(
        venue="binance_usdm",
        symbol=f"{base}USDT",
        base_asset=base,
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        underlying_type="COIN",
        quote_volume_usdt=volume,
    )


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
    assert depth_update_gap(previous_final_id=10, first_id=12, final_id=13) == "gap"
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
        tuple(_contract("BTC" if False else f"C{rank}") for rank in range(1, 101) if rank not in {10, 50})
        + (_contract("WBTC", "5"),),
        count_kind="synthetic",
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
        count_kind="synthetic",
    )
    assert "FOOUSDT" not in collision.queued_symbols
    missing_volume = evaluate_top100(
        _page(tuple(_asset(rank, f"C{rank}") for rank in range(1, 101))),
        tuple(_contract(f"C{rank}", None) for rank in range(1, 101)),
        count_kind="synthetic",
    )
    assert missing_volume.unique_queued_discovery_contracts == 0
    assert missing_volume.insufficient_evidence_rejections == 100


def test_multiplier_symbol_is_not_stripped_or_merged() -> None:
    assets = tuple(_asset(rank, "PEPE" if rank == 1 else f"C{rank}") for rank in range(1, 101))
    counts = evaluate_top100(
        _page(assets),
        (_contract("1000PEPE"),) + tuple(_contract(f"C{rank}") for rank in range(2, 101)),
        count_kind="synthetic",
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
    )
    assert summary["transport_label"] == "synthetic_transport"
    assert summary["top100"]["membership_status"] == "verified"
    assert summary["top100"]["scan_attempts"] == 0
    assert summary["coinglass"] == "not_called_no_authorized_no_overage_entitlement"


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
