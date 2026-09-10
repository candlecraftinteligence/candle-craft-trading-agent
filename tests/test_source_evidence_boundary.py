"""SOURCE_EVIDENCE_BOUNDARY: executable source-claim contract and producer proofs.

Synthetic fixtures and mocked transports only. No live exchange calls and no
live or existing scan database is read. Descriptors constructed here remain
unbound; harness-observed fetch times are not an owned acquisition chain.

Evidence labels below are test documentation, not application enums.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import pytest

from app.analytics.evidence_contract import UNAVAILABLE, evidence_contract_payload
from app.analytics.outcome_ownership import project_outcome_ownership
from app.backtesting.strategy_replay import ReplayConfig, ReplayOutcome
from app.cache.market_data_cache import CachedMarketDataClient, MarketDataCache
from app.data.exchange_clients import BinanceFuturesClient
from app.data.timeframes import resample_ohlcv_candles
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import SetupLifecycleService
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunResult, ScannerRunner, ScannerSymbolResult
from app.research.evaluation_policy import (
    build_replay_evaluation_policy,
    build_runtime_evaluation_policy,
)
from app.research.source_evidence import (
    BINDING_STATUS_UNBOUND,
    CACHE_KIND_EXPIRY_REFETCH,
    CACHE_KIND_HIT,
    CACHE_KIND_MISS,
    CACHE_KIND_NOT_APPLICABLE,
    CACHE_KIND_UNAVAILABLE,
    CLAIM_DECLARED,
    CLAIM_NOT_APPLICABLE,
    CLAIM_UNAVAILABLE,
    CONTRACT_FORMAT_VERSION,
    DELIVERY_ADAPTER_RETURN,
    DELIVERY_CACHE_RETURN,
    DELIVERY_DIRECT_SUPPLIED,
    DELIVERY_TRANSFORMED_RESAMPLED,
    DELIVERY_UNKNOWN,
    ORIGIN_ACQUISITION_CLAIM,
    ORIGIN_EXTERNALLY_SUPPLIED,
    ORIGIN_HISTORICAL_RECONSTRUCTION,
    ORIGIN_SYNTHETIC_FIXTURE,
    PURPOSE_DIRECT_EVALUATION_RECONSTRUCTION,
    PURPOSE_REPLAY_EXPERIMENT,
    PURPOSE_SCANNER_OBSERVATION,
    PURPOSE_UNSPECIFIED,
    REPRESENTATION_EPOCH_MS,
    REPRESENTATION_UTC_ISO8601,
    SUPPORT_UNAVAILABLE,
    SUPPORT_UNVERIFIED_DECLARATION,
    TEMPORAL_CACHE_BOOKKEEPING,
    TEMPORAL_CANDLE_CLOSE,
    TEMPORAL_DECLARED_ACQUISITION_OBSERVATION,
    TEMPORAL_LOGICAL_DECISION_CUTOFF,
    TRANSFORMATION_SYNTHETIC_2D_RESAMPLE,
    SourceEvidenceError,
    assess_source_evidence,
    build_source_evidence_descriptor,
    parse_source_evidence_payload,
)
from app.storage.database import SCHEMA_VERSION, open_initialized_database
from app.storage.repositories import store_scan_result
from tests.test_evaluation_semantics_contract import (
    EVIDENCE_MALFORMED,
    EVIDENCE_NORMAL_PRODUCER,
    EVIDENCE_SOURCE_ONLY,
    EVIDENCE_SUPPLIED_STATE,
    TIMEFRAME,
    _both_entry,
    _close,
    _decision,
    _no_touch,
    _record,
    _replay,
    _runtime,
)
from tests.test_scanner_runner import FakeExchangeClient, _config, _flat_candles

EVIDENCE_MIXED = "mixed planted-record plus ordinary service evaluation"
EVIDENCE_SYNTHETIC_HARNESS = "synthetic harness observation; not an owned acquisition chain"

REPO_ROOT = Path(__file__).resolve().parents[1]
DAY_MS = 86_400_000
REQUIRED_ACQUISITION_LIMITATIONS = (
    "local_completion_is_not_venue_publication_time",
    "class_identity_is_not_venue_proof",
    "config_exchange_is_not_provenance",
    "cutoff_is_not_possession",
)
FORBIDDEN_SOURCE_FIELDS = (
    "admission_id",
    "evaluation_episode_id",
    "evaluation_policy_id",
    "policy_hash",
    "source_namespace",
    "source_evidence_id",
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _descriptor(**overrides: Any):
    payload: dict[str, Any] = {
        "evaluation_purpose": PURPOSE_SCANNER_OBSERVATION,
        "input_origin": ORIGIN_SYNTHETIC_FIXTURE,
        "delivery_path": DELIVERY_DIRECT_SUPPLIED,
    }
    payload.update(overrides)
    return build_source_evidence_descriptor(**payload)


def _scan_config(**overrides: object):
    data: dict[str, object] = {
        "cache_enabled": False,
        "btc_context_enabled": False,
        "btc_d_context_enabled": False,
        "global_context_enabled": False,
        "market_regime_enabled": False,
        "macro_event_context_enabled": False,
    }
    data.update(overrides)
    return _config(["BTCUSDT"], **data)


class ControllableDatetimeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current
        self.samples: list[datetime] = []

    def __call__(self) -> datetime:
        self.samples.append(self.current)
        return self.current

    def advance(self, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class ClockAdvancingClient(FakeExchangeClient):
    def __init__(self, candles_by_symbol: dict[str, list[dict[str, Decimal | int]]], clock: ControllableDatetimeClock) -> None:
        super().__init__(candles_by_symbol, failing_timeframes={"2d"})
        self.clock = clock
        self.kline_started_at: list[datetime] = []
        self.kline_completed_at: list[datetime] = []
        self.exchange_name = "injected_fixture_client"

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
        started = self.clock()
        self.kline_started_at.append(started)
        self.clock.advance(30)
        result = await super().get_klines(symbol, interval, limit)
        self.kline_completed_at.append(self.clock())
        return result


class CacheClock:
    def __init__(self, current: float = 1_000.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current


class FetchAdvancingClient:
    exchange_name = "injected_fixture_client"

    def __init__(
        self,
        clock: CacheClock,
        *,
        advance: float = 25.0,
        payload: list[dict[str, Decimal | int | str]] | None = None,
    ) -> None:
        self.clock = clock
        self.advance = advance
        self.payload = payload or [
            {
                "symbol": "BTCUSDT",
                "timestamp": 1,
                "interval": "15m",
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal("100"),
                "volume": Decimal("10"),
            }
        ]
        self.calls = 0
        self.completed_at: list[float] = []

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int | str]]:
        self.calls += 1
        self.clock.current += self.advance
        self.completed_at.append(self.clock.current)
        return list(self.payload)


def test_schema_remains_v25_without_source_policy_or_admission_binding(tmp_path: Path) -> None:
    path = tmp_path / "source-evidence-schema.sqlite"
    with open_initialized_database(path) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        sqlite_schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        progress_cols = {row[1] for row in connection.execute("PRAGMA table_info(setup_lifecycle_outcome_progress)")}
        analytics_cols = {row[1] for row in connection.execute("PRAGMA table_info(setup_outcome_analytics)")}
        replay_cols = {row[1] for row in connection.execute("PRAGMA table_info(replay_results)")}
        scan_cols = {row[1] for row in connection.execute("PRAGMA table_info(scan_runs)")}
        progress_unique = connection.execute(
            "PRAGMA index_info(sqlite_autoindex_setup_lifecycle_outcome_progress_1)"
        ).fetchall()
        analytics_unique = connection.execute(
            "PRAGMA index_info(sqlite_autoindex_setup_outcome_analytics_1)"
        ).fetchall()
    assert user_version == SCHEMA_VERSION == 25
    assert sqlite_schema_version != user_version
    assert sqlite_schema_version > 0
    assert len(tables) >= 13
    for forbidden in FORBIDDEN_SOURCE_FIELDS:
        assert forbidden not in progress_cols
        assert forbidden not in analytics_cols
        assert forbidden not in replay_cols
        assert forbidden not in scan_cols
    assert "admitted_evaluation_episodes" not in tables
    assert "source_evidence" not in tables
    assert [row[2] for row in progress_unique] == ["lifecycle_id", "plan_identity"]
    assert [row[2] for row in analytics_unique] == ["lifecycle_id", "final_outcome"]


def test_descriptor_is_immutable_unbound_and_does_not_mint_a_source_id() -> None:
    first = _descriptor(
        evaluation_purpose=PURPOSE_SCANNER_OBSERVATION,
        input_origin=ORIGIN_SYNTHETIC_FIXTURE,
        delivery_path=DELIVERY_DIRECT_SUPPLIED,
    )
    second = parse_source_evidence_payload(first.to_canonical_dict())
    reversed_keys = parse_source_evidence_payload(
        {
            "temporal_claims": deepcopy(first.to_canonical_dict()["temporal_claims"]),
            "acquisition_claim_limitations": [],
            "config_exchange_label": {"status": CLAIM_UNAVAILABLE, "value": None},
            "adapter_class_label": {"status": CLAIM_UNAVAILABLE, "value": None},
            "venue_label": {"status": CLAIM_UNAVAILABLE, "value": None},
            "transformation_kind": "not_applicable",
            "cache_delivery_kind": CACHE_KIND_NOT_APPLICABLE,
            "delivery_path": DELIVERY_DIRECT_SUPPLIED,
            "input_origin": ORIGIN_SYNTHETIC_FIXTURE,
            "evaluation_purpose": PURPOSE_SCANNER_OBSERVATION,
            "contract_format_version": CONTRACT_FORMAT_VERSION,
        }
    )
    assert first.binding_status == second.binding_status == BINDING_STATUS_UNBOUND
    assert first.canonical_bytes == second.canonical_bytes == reversed_keys.canonical_bytes
    assert "source_id" not in first.to_canonical_dict()
    assert "source_namespace" not in first.to_canonical_dict()
    exported = first.to_canonical_dict()
    exported["input_origin"] = ORIGIN_ACQUISITION_CLAIM
    exported["temporal_claims"][TEMPORAL_LOGICAL_DECISION_CUTOFF]["status"] = CLAIM_DECLARED
    assert first.to_canonical_dict()["input_origin"] == ORIGIN_SYNTHETIC_FIXTURE
    assert isinstance(first.payload, MappingProxyType)
    with pytest.raises(TypeError):
        first.payload["evaluation_purpose"] = PURPOSE_REPLAY_EXPERIMENT  # type: ignore[index]
    assessment = assess_source_evidence(first)
    assert assessment.binding_status == BINDING_STATUS_UNBOUND
    assert assessment.witnessed_acquisition_chain == SUPPORT_UNAVAILABLE
    assert assessment.possession_at_logical_cutoff == SUPPORT_UNAVAILABLE
    assert "equal_declared_fields_are_not_opportunity_identity" in assessment.limitation_codes
    assert EVIDENCE_SUPPLIED_STATE


def test_structural_validity_does_not_certify_declared_receipt_times() -> None:
    declared = _descriptor(
        evaluation_purpose=PURPOSE_SCANNER_OBSERVATION,
        input_origin=ORIGIN_ACQUISITION_CLAIM,
        delivery_path=DELIVERY_ADAPTER_RETURN,
        venue_label="binance",
        adapter_class_label="app.data.exchange_clients.binance_futures.BinanceFuturesClient",
        config_exchange_label="binance",
        acquisition_claim_limitations=REQUIRED_ACQUISITION_LIMITATIONS,
        temporal_claims={
            TEMPORAL_LOGICAL_DECISION_CUTOFF: {
                "status": CLAIM_DECLARED,
                "value": "2026-01-01T00:00:00+00:00",
                "representation": REPRESENTATION_UTC_ISO8601,
            },
            TEMPORAL_DECLARED_ACQUISITION_OBSERVATION: {
                "status": CLAIM_DECLARED,
                "value": "2026-01-01T00:00:30+00:00",
                "representation": REPRESENTATION_UTC_ISO8601,
            },
        },
    )
    assessment = assess_source_evidence(declared)
    assert declared.binding_status == BINDING_STATUS_UNBOUND
    assert assessment.factual_support["temporal_claims.declared_acquisition_observation"] == (
        SUPPORT_UNVERIFIED_DECLARATION
    )
    assert assessment.authentic_venue_provenance == SUPPORT_UNAVAILABLE
    assert assessment.admission_authority == SUPPORT_UNAVAILABLE
    assert "declared_acquisition_observation_is_unverified" in assessment.limitation_codes
    assert EVIDENCE_SUPPLIED_STATE


def test_malformed_authority_policy_and_conflict_inputs_are_rejected() -> None:
    valid = _descriptor().to_canonical_dict()
    with pytest.raises(SourceEvidenceError, match="authority_escape_hatch_forbidden"):
        parse_source_evidence_payload({**valid, "authoritative": True})
    with pytest.raises(SourceEvidenceError, match="authority_escape_hatch_forbidden"):
        parse_source_evidence_payload({**valid, "source_verified": True})
    with pytest.raises(SourceEvidenceError, match="authority_escape_hatch_forbidden"):
        parse_source_evidence_payload({**valid, "evaluation_policy_id": "eval-policy-abc"})
    with pytest.raises(SourceEvidenceError, match="authority_escape_hatch_forbidden"):
        parse_source_evidence_payload({**valid, "source_namespace": "runtime"})
    with pytest.raises(SourceEvidenceError, match="unknown_field"):
        parse_source_evidence_payload({**valid, "git_sha": "abc"})
    with pytest.raises(SourceEvidenceError, match="unresolved_placeholder"):
        parse_source_evidence_payload({**valid, "evaluation_purpose": "unknown"})
    with pytest.raises(SourceEvidenceError, match="cache_delivery_kind_not_applicable_off_cache_path"):
        parse_source_evidence_payload({**valid, "cache_delivery_kind": CACHE_KIND_HIT})
    missing = dict(valid)
    missing.pop("delivery_path")
    with pytest.raises(SourceEvidenceError, match="missing_field:delivery_path"):
        parse_source_evidence_payload(missing)
    with pytest.raises(SourceEvidenceError, match="acquisition_claim_missing_required_limitations"):
        _descriptor(
            input_origin=ORIGIN_ACQUISITION_CLAIM,
            delivery_path=DELIVERY_ADAPTER_RETURN,
            acquisition_claim_limitations=("injected_transport_permitted",),
        )
    with pytest.raises(SourceEvidenceError, match="naive_datetime_forbidden"):
        _descriptor(
            temporal_claims={
                TEMPORAL_LOGICAL_DECISION_CUTOFF: {
                    "status": CLAIM_DECLARED,
                    "value": "2026-01-01T00:00:00",
                    "representation": REPRESENTATION_UTC_ISO8601,
                }
            }
        )
    assert EVIDENCE_MALFORMED


def test_conflicting_venue_labels_remain_unresolved_without_repair() -> None:
    descriptor = _descriptor(
        evaluation_purpose=PURPOSE_SCANNER_OBSERVATION,
        input_origin=ORIGIN_SYNTHETIC_FIXTURE,
        delivery_path=DELIVERY_ADAPTER_RETURN,
        venue_label="binance",
        config_exchange_label="bybit",
        adapter_class_label="ClockAdvancingClient",
    )
    assessment = assess_source_evidence(descriptor)
    assert descriptor.to_canonical_dict()["venue_label"]["value"] == "binance"
    assert descriptor.to_canonical_dict()["config_exchange_label"]["value"] == "bybit"
    assert assessment.claim_conflicts == ("venue_label_differs_from_config_exchange_label",)
    assert assessment.authentic_venue_provenance == SUPPORT_UNAVAILABLE
    assert EVIDENCE_SUPPLIED_STATE


def test_runner_resolves_logical_cutoff_before_later_acquisition_completes() -> None:
    cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = ControllableDatetimeClock(cutoff)
    client = ClockAdvancingClient({"BTCUSDT": _flat_candles()}, clock)
    result = run(ScannerRunner(exchange_client=client, clock=clock).run(_scan_config()))
    assert result.config.decision_timestamp == cutoff
    assert client.kline_started_at
    assert client.kline_completed_at
    assert min(client.kline_started_at) >= cutoff
    assert min(client.kline_completed_at) > cutoff
    symbol = result.results[0]
    assert symbol.lifecycle_decision_timestamp == cutoff
    dumped = symbol.model_dump()
    assert "lifecycle_execution_candles" not in dumped
    assert "lifecycle_decision_timestamp" not in dumped
    candles = symbol.lifecycle_execution_candles or ()
    sample = candles[0] if candles else {"timestamp": 0}
    open_ms = int(sample["timestamp"] if isinstance(sample, dict) else sample.timestamp)
    first_close_ms = open_ms + (15 * 60_000)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    assert first_close_ms <= cutoff_ms
    descriptor = _descriptor(
        evaluation_purpose=PURPOSE_SCANNER_OBSERVATION,
        input_origin=ORIGIN_SYNTHETIC_FIXTURE,
        delivery_path=DELIVERY_ADAPTER_RETURN,
        adapter_class_label="ClockAdvancingClient",
        config_exchange_label="binance",
        temporal_claims={
            TEMPORAL_LOGICAL_DECISION_CUTOFF: {
                "status": CLAIM_DECLARED,
                "value": _iso(cutoff),
                "representation": REPRESENTATION_UTC_ISO8601,
            },
            TEMPORAL_DECLARED_ACQUISITION_OBSERVATION: {
                "status": CLAIM_DECLARED,
                "value": _iso(min(client.kline_completed_at)),
                "representation": REPRESENTATION_UTC_ISO8601,
            },
            TEMPORAL_CANDLE_CLOSE: {
                "status": CLAIM_DECLARED,
                "value": first_close_ms,
                "representation": REPRESENTATION_EPOCH_MS,
            },
        },
    )
    assessment = assess_source_evidence(descriptor)
    assert assessment.possession_at_logical_cutoff == SUPPORT_UNAVAILABLE
    assert assessment.witnessed_acquisition_chain == SUPPORT_UNAVAILABLE
    assert "logical_cutoff_is_not_possession" in assessment.limitation_codes
    assert EVIDENCE_NORMAL_PRODUCER
    assert EVIDENCE_SYNTHETIC_HARNESS


def test_cache_created_at_is_sampled_before_fetch_completes() -> None:
    clock = CacheClock(1_000.0)
    client = FetchAdvancingClient(clock)
    cache = MarketDataCache(now=clock)
    cached = CachedMarketDataClient(client, cache)
    returned = run(cached.get_klines("BTCUSDT", "15m", 1))
    entry = next(iter(cache._entries.values()))
    assert entry["created_at"] == 1_000.0
    assert client.completed_at == [1_025.0]
    assert entry["created_at"] < client.completed_at[0]
    assert returned[0]["close"] == Decimal("100")
    with pytest.raises(SourceEvidenceError, match="epoch_ms_must_be_int"):
        _descriptor(
            evaluation_purpose=PURPOSE_SCANNER_OBSERVATION,
            input_origin=ORIGIN_SYNTHETIC_FIXTURE,
            delivery_path=DELIVERY_CACHE_RETURN,
            cache_delivery_kind=CACHE_KIND_MISS,
            temporal_claims={
                TEMPORAL_CACHE_BOOKKEEPING: {
                    "status": CLAIM_DECLARED,
                    "value": "1000",
                    "representation": REPRESENTATION_EPOCH_MS,
                }
            },
        )
    bookkeeping = _descriptor(
        evaluation_purpose=PURPOSE_SCANNER_OBSERVATION,
        input_origin=ORIGIN_SYNTHETIC_FIXTURE,
        delivery_path=DELIVERY_CACHE_RETURN,
        cache_delivery_kind=CACHE_KIND_MISS,
        temporal_claims={
            TEMPORAL_CACHE_BOOKKEEPING: {
                "status": CLAIM_DECLARED,
                "value": 1000,
                "representation": REPRESENTATION_EPOCH_MS,
            },
            TEMPORAL_DECLARED_ACQUISITION_OBSERVATION: {
                "status": CLAIM_DECLARED,
                "value": 1025,
                "representation": REPRESENTATION_EPOCH_MS,
            },
        },
    )
    assessment = assess_source_evidence(bookkeeping)
    assert assessment.factual_support["temporal_claims.cache_bookkeeping_time"] == SUPPORT_UNVERIFIED_DECLARATION
    assert "cache_bookkeeping_is_not_receipt" in assessment.limitation_codes
    assert EVIDENCE_NORMAL_PRODUCER
    assert EVIDENCE_SYNTHETIC_HARNESS


def test_cache_hit_miss_and_expiry_are_distinct_delivery_histories() -> None:
    clock = CacheClock(1_000.0)
    client = FetchAdvancingClient(clock, advance=0.0)
    cache = MarketDataCache(ttl_seconds=10, now=clock)
    cached = CachedMarketDataClient(client, cache)
    first = run(cached.get_klines("BTCUSDT", "15m", 1))
    second = run(cached.get_klines("BTCUSDT", "15m", 1))
    clock.current = 1_020.0
    third = run(cached.get_klines("BTCUSDT", "15m", 1))
    assert first == second == third
    assert client.calls == 2
    assert cache.stats()["misses"] == 2
    assert cache.stats()["hits"] == 1
    assert cache.stats()["expired"] == 1
    miss = _descriptor(
        delivery_path=DELIVERY_CACHE_RETURN,
        cache_delivery_kind=CACHE_KIND_MISS,
        temporal_claims={
            TEMPORAL_CACHE_BOOKKEEPING: {
                "status": CLAIM_DECLARED,
                "value": 1000,
                "representation": REPRESENTATION_EPOCH_MS,
            }
        },
    )
    hit = _descriptor(
        delivery_path=DELIVERY_CACHE_RETURN,
        cache_delivery_kind=CACHE_KIND_HIT,
        temporal_claims={
            TEMPORAL_CACHE_BOOKKEEPING: {
                "status": CLAIM_DECLARED,
                "value": 1000,
                "representation": REPRESENTATION_EPOCH_MS,
            }
        },
    )
    expiry = _descriptor(
        delivery_path=DELIVERY_CACHE_RETURN,
        cache_delivery_kind=CACHE_KIND_EXPIRY_REFETCH,
        temporal_claims={
            TEMPORAL_CACHE_BOOKKEEPING: {
                "status": CLAIM_DECLARED,
                "value": 1020,
                "representation": REPRESENTATION_EPOCH_MS,
            }
        },
    )
    assert miss.canonical_bytes != hit.canonical_bytes != expiry.canonical_bytes
    hit_assessment = assess_source_evidence(hit)
    assert "cache_hit_is_not_fresh_exchange_observation" in hit_assessment.limitation_codes
    assert hit_assessment.witnessed_acquisition_chain == SUPPORT_UNAVAILABLE
    assert EVIDENCE_NORMAL_PRODUCER


def test_legacy_file_cache_hit_remains_unverified(tmp_path: Path) -> None:
    cache_path = tmp_path / "market_cache.json"
    first_client = FetchAdvancingClient(CacheClock(1_000.0), advance=0.0)
    first_cache = MarketDataCache(file_path=cache_path, now=CacheClock(1_000.0))
    first = run(CachedMarketDataClient(first_client, first_cache).get_klines("BTCUSDT", "15m", 1))
    stored = json.loads(cache_path.read_text(encoding="utf-8"))
    stored_entry = next(iter(stored["entries"].values()))
    assert "created_at" in stored_entry
    assert "acquisition_completed_at" not in stored_entry
    second_client = FetchAdvancingClient(CacheClock(5_000.0), advance=0.0)
    second_cache = MarketDataCache(file_path=cache_path, now=CacheClock(1_001.0))
    second = run(CachedMarketDataClient(second_client, second_cache).get_klines("BTCUSDT", "15m", 1))
    assert second == first
    assert first_client.calls == 1
    assert second_client.calls == 0
    descriptor = _descriptor(
        delivery_path=DELIVERY_CACHE_RETURN,
        cache_delivery_kind=CACHE_KIND_HIT,
        temporal_claims={
            TEMPORAL_CACHE_BOOKKEEPING: {
                "status": CLAIM_DECLARED,
                "value": 1000,
                "representation": REPRESENTATION_EPOCH_MS,
            },
            TEMPORAL_DECLARED_ACQUISITION_OBSERVATION: {
                "status": CLAIM_UNAVAILABLE,
                "value": None,
                "representation": None,
            },
        },
    )
    assessment = assess_source_evidence(descriptor)
    assert assessment.factual_support["temporal_claims.declared_acquisition_observation"] == SUPPORT_UNAVAILABLE
    assert "cache_hit_is_not_fresh_exchange_observation" in assessment.limitation_codes
    restarted = _descriptor(
        delivery_path=DELIVERY_CACHE_RETURN,
        cache_delivery_kind=CACHE_KIND_UNAVAILABLE,
    )
    assert "legacy_cache_entry_lacks_acquisition_observation" in assess_source_evidence(restarted).limitation_codes
    assert EVIDENCE_NORMAL_PRODUCER


def test_injected_fixture_client_does_not_inherit_binance_config_provenance() -> None:
    client = ClockAdvancingClient(
        {"BTCUSDT": _flat_candles()},
        ControllableDatetimeClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    result = run(
        ScannerRunner(exchange_client=client).run(
            _scan_config(exchange="binance", cache_enabled=True, cache_ttl_seconds=60)
        )
    )
    assert result.config.exchange == "binance"
    assert client.requested_klines
    assert all(not isinstance(item, BinanceFuturesClient) for item in (client,))
    stats = result.cache_stats
    assert stats.get("misses", 0) >= 1
    descriptor = _descriptor(
        evaluation_purpose=PURPOSE_SCANNER_OBSERVATION,
        input_origin=ORIGIN_SYNTHETIC_FIXTURE,
        delivery_path=DELIVERY_CACHE_RETURN,
        cache_delivery_kind=CACHE_KIND_MISS,
        venue_label="binance",
        config_exchange_label="binance",
        adapter_class_label="ClockAdvancingClient",
    )
    assessment = assess_source_evidence(descriptor)
    assert assessment.authentic_venue_provenance == SUPPORT_UNAVAILABLE
    assert "fixture_is_not_live_market_provenance" in assessment.limitation_codes
    assert "config_exchange_is_not_provenance" in assessment.limitation_codes
    assert EVIDENCE_NORMAL_PRODUCER


def test_binance_adapter_route_with_mocked_http_is_still_synthetic() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(
            200,
            json=[
                [
                    1_710_000_000_000,
                    "100.0",
                    "110.0",
                    "95.0",
                    "105.0",
                    "12.5",
                    1_710_000_059_999,
                    "1312.5",
                    42,
                    "6.0",
                    "630.0",
                    "0",
                ]
            ],
        )

    async def scenario() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://binance.test")
        client = BinanceFuturesClient(http_client=http_client, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        candles = await client.get_klines("btcusdt", "1m", 1)
        await client._http_client.aclose()
        assert requested == ["/fapi/v1/klines"]
        assert candles[0].exchange == "binance_futures"
        assert candles[0].symbol == "BTCUSDT"

    run(scenario())
    descriptor = _descriptor(
        evaluation_purpose=PURPOSE_SCANNER_OBSERVATION,
        input_origin=ORIGIN_ACQUISITION_CLAIM,
        delivery_path=DELIVERY_ADAPTER_RETURN,
        venue_label="binance_futures",
        adapter_class_label="BinanceFuturesClient",
        config_exchange_label="binance",
        acquisition_claim_limitations=(
            *REQUIRED_ACQUISITION_LIMITATIONS,
            "injected_transport_permitted",
            "normalized_exchange_field_is_not_receipt",
        ),
    )
    assessment = assess_source_evidence(descriptor)
    assert assessment.authentic_venue_provenance == SUPPORT_UNAVAILABLE
    assert "class_identity_is_not_venue_proof" in assessment.limitation_codes
    assert EVIDENCE_NORMAL_PRODUCER
    assert EVIDENCE_SYNTHETIC_HARNESS


def test_runtime_replay_purpose_and_source_are_orthogonal_to_policy_identity(tmp_path: Path) -> None:
    candles = [_no_touch(0, "long"), _both_entry(1)]
    runtime = _runtime(tmp_path, _record(), candles)
    replay = _replay(candles, direction="long")
    runtime_policy_a = build_runtime_evaluation_policy(execution_timeframe=TIMEFRAME)
    runtime_policy_b = build_runtime_evaluation_policy(execution_timeframe=TIMEFRAME)
    replay_policy = build_replay_evaluation_policy(
        config=ReplayConfig(execution_timeframe=TIMEFRAME),
        mode="swing",
    )
    scanner_source = _descriptor(
        evaluation_purpose=PURPOSE_SCANNER_OBSERVATION,
        input_origin=ORIGIN_SYNTHETIC_FIXTURE,
        delivery_path=DELIVERY_DIRECT_SUPPLIED,
    )
    replay_source = _descriptor(
        evaluation_purpose=PURPOSE_REPLAY_EXPERIMENT,
        input_origin=ORIGIN_HISTORICAL_RECONSTRUCTION,
        delivery_path=DELIVERY_DIRECT_SUPPLIED,
    )
    reconstruction_source = _descriptor(
        evaluation_purpose=PURPOSE_DIRECT_EVALUATION_RECONSTRUCTION,
        input_origin=ORIGIN_EXTERNALLY_SUPPLIED,
        delivery_path=DELIVERY_DIRECT_SUPPLIED,
    )
    assert runtime_policy_a.policy_id == runtime_policy_b.policy_id
    assert runtime_policy_a.policy_id != replay_policy.policy_id
    assert scanner_source.canonical_bytes != replay_source.canonical_bytes
    assert reconstruction_source.to_canonical_dict()["evaluation_purpose"] == PURPOSE_DIRECT_EVALUATION_RECONSTRUCTION
    assert runtime.progress is not None
    assert replay.filled is True
    # Source declarations must not be required to agree with evaluator outputs.
    assert runtime.progress.entry_at is not None
    assert replay.outcome != ReplayOutcome.NOT_FILLED
    assert "source_evidence" not in runtime.progress.model_dump()
    assert EVIDENCE_SUPPLIED_STATE


def test_lifecycle_service_has_no_source_context_and_omits_evaluator_inputs(tmp_path: Path) -> None:
    db_path = tmp_path / "lifecycle-source.sqlite"
    record = _record()
    baseline = _no_touch(0, "long")
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "invalidation": "Closed structure beyond the stored stop invalidates the plan.",
            }
        },
        valid_strategy_modes=("swing",),
        lifecycle_execution_candles=(baseline, _both_entry(1)),
        lifecycle_execution_timeframe=TIMEFRAME,
        lifecycle_decision_timestamp=_close(1),
    )
    dumped = symbol_result.model_dump()
    assert "lifecycle_execution_candles" not in dumped
    assert "lifecycle_decision_timestamp" not in dumped
    assert "lifecycle_execution_timeframe" not in dumped
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        service = SetupLifecycleService(db_path)
        updated = service.apply_to_symbol_result(
            symbol_result,
            repository=repository,
            scan_run_id="scan-source",
            now=_decision(1),
        )
    assert updated.lifecycle_outcome_progress is not None
    progress_dump = updated.lifecycle_outcome_progress.model_dump()
    for forbidden in FORBIDDEN_SOURCE_FIELDS:
        assert forbidden not in progress_dump
        assert forbidden not in dumped
    source = inspect.getsource(SetupLifecycleService._apply_to_symbol_result_with_meta)
    assert "if final_record is not None and execution_candles is not None" in source
    assert "source_evidence" not in source
    assert EVIDENCE_MIXED


def test_none_execution_candles_skip_evaluator_without_fabricating_source(tmp_path: Path) -> None:
    db_path = tmp_path / "none-candles.sqlite"
    record = _record(lifecycle_id="life-none")
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "invalidation": "Closed structure beyond the stored stop invalidates the plan.",
            }
        },
        valid_strategy_modes=("swing",),
    )
    assert symbol_result.lifecycle_execution_candles is None
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        updated = SetupLifecycleService(db_path).apply_to_symbol_result(
            symbol_result,
            repository=repository,
            scan_run_id="scan-none",
            now=_decision(0),
        )
    assert updated.lifecycle_outcome_progress is None
    assert EVIDENCE_NORMAL_PRODUCER


def test_not_run_and_fetch_failure_do_not_create_source_observations(tmp_path: Path) -> None:
    failing = FakeExchangeClient({"FAILUSDT": _flat_candles()}, failing_symbols={"FAILUSDT"})
    failed = run(
        ScannerRunner(exchange_client=failing).run(
            _config(
                ["FAILUSDT"],
                cache_enabled=False,
                btc_context_enabled=False,
                btc_d_context_enabled=False,
                global_context_enabled=False,
                market_regime_enabled=False,
                macro_event_context_enabled=False,
            )
        )
    )
    assert failed.results[0].status in (ScannerPipelineStatus.SCAN_ERROR, ScannerPipelineStatus.FAILED)
    assert "source_evidence" not in failed.results[0].model_dump()
    not_run = ScannerSymbolResult(
        symbol="ETHUSDT",
        status=ScannerPipelineStatus.NOT_RUN,
        status_history=(ScannerPipelineStatus.NOT_RUN,),
        not_run_reason="omitted_from_queue",
        error_message="Symbol was not run: omitted_from_queue.",
    )
    config = _config(["ETHUSDT"], cache_enabled=False)
    scan = ScannerRunResult(
        config=config,
        results=(not_run,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )
    service = SetupLifecycleService(tmp_path / "not-run.sqlite")
    processed = service.apply_to_run_result(scan, scan_run_id="scan-not-run", now=_decision(0))
    assert processed.scanner_process_summary["skipped_not_run_symbols"] == 1
    assert processed.results[0].lifecycle_outcome_progress is None
    empty = _runtime(tmp_path, _record(lifecycle_id="life-empty"), [], db_name="empty.sqlite")
    assert empty.progress is not None
    assert empty.progress.integrity_status == "Unverified"
    unknown_delivery = _descriptor(
        evaluation_purpose=PURPOSE_UNSPECIFIED,
        input_origin=ORIGIN_EXTERNALLY_SUPPLIED,
        delivery_path=DELIVERY_UNKNOWN,
    )
    assessment = assess_source_evidence(unknown_delivery)
    assert assessment.factual_support["delivery_path"] == SUPPORT_UNAVAILABLE
    assert "delivery_path_unavailable" in assessment.limitation_codes
    assert EVIDENCE_NORMAL_PRODUCER


def test_synthetic_2d_resample_is_transformation_not_a_market_fixture() -> None:
    candles = [
        {
            "timestamp": 0,
            "open": Decimal("100"),
            "high": Decimal("110"),
            "low": Decimal("95"),
            "close": Decimal("105"),
            "volume": Decimal("10"),
        },
        {
            "timestamp": DAY_MS,
            "open": Decimal("105"),
            "high": Decimal("115"),
            "low": Decimal("101"),
            "close": Decimal("112"),
            "volume": Decimal("20"),
        },
    ]
    resampled = resample_ohlcv_candles(candles, decision_timestamp=2 * DAY_MS)
    assert resampled[0]["interval"] == "2d"
    descriptor = _descriptor(
        evaluation_purpose=PURPOSE_SCANNER_OBSERVATION,
        input_origin=ORIGIN_SYNTHETIC_FIXTURE,
        delivery_path=DELIVERY_TRANSFORMED_RESAMPLED,
        transformation_kind=TRANSFORMATION_SYNTHETIC_2D_RESAMPLE,
        venue_label="binance_futures",
    )
    assessment = assess_source_evidence(descriptor)
    assert descriptor.to_canonical_dict()["transformation_kind"] == TRANSFORMATION_SYNTHETIC_2D_RESAMPLE
    assert descriptor.to_canonical_dict()["input_origin"] == ORIGIN_SYNTHETIC_FIXTURE
    assert "resample_is_not_new_venue_observation" in assessment.limitation_codes
    assert assessment.authentic_venue_provenance == SUPPORT_UNAVAILABLE
    assert EVIDENCE_NORMAL_PRODUCER


def test_stored_scan_payload_cannot_recreate_omitted_evaluator_inputs(tmp_path: Path) -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()}, failing_timeframes={"2d"})
    result = run(ScannerRunner(exchange_client=client).run(_scan_config()))
    symbol = result.results[0]
    assert symbol.lifecycle_execution_candles is not None
    run_id = store_scan_result(tmp_path / "scan-source.sqlite", result, inline_raw_payload=True)
    with open_initialized_database(tmp_path / "scan-source.sqlite") as connection:
        raw = connection.execute(
            "SELECT raw_payload_json FROM scan_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
    payload = json.loads(raw)
    encoded = json.dumps(payload)
    assert "lifecycle_execution_candles" not in encoded
    assert "lifecycle_decision_timestamp" not in encoded
    assert EVIDENCE_NORMAL_PRODUCER


def test_production_modules_do_not_consume_source_or_policy_contracts() -> None:
    hits: list[str] = []
    for folder in (REPO_ROOT / "app", REPO_ROOT / "scripts"):
        for path in folder.rglob("*.py"):
            if path.name in {"source_evidence.py", "evaluation_policy.py"}:
                continue
            text = path.read_text(encoding="utf-8")
            if "research.source_evidence" in text or "from app.research import source_evidence" in text:
                hits.append(str(path.relative_to(REPO_ROOT)))
            if "research.evaluation_policy" in text or "from app.research import evaluation_policy" in text:
                hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == []
    init_text = (REPO_ROOT / "app/research/__init__.py").read_text(encoding="utf-8")
    assert "source_evidence" not in init_text
    assert "evaluation_policy" not in init_text
    module_source = Path("app/research/source_evidence.py").read_text(encoding="utf-8")
    assert "datetime.now" not in module_source
    assert "time.time" not in module_source
    assert "os.environ" not in module_source
    assert inspect.isfunction(evaluate_closed_candle_outcomes)


def test_claim_preservation_admission_and_expectancy_remain_unproven() -> None:
    contract = evidence_contract_payload()
    assert contract["unique_trade_count"]["status"] == UNAVAILABLE
    payload = project_outcome_ownership(
        lifecycle_records=[],
        progress_rows=[],
        provenance={"synthetic": True, "coverage_complete": True},
    )
    assert payload["unavailable_metrics"]["unique_trade_count"]["status"] == UNAVAILABLE
    assert payload["evaluations"] == [] or payload["evaluations"][0]["evaluation_context"]["complete"] is False
    unspecified = _descriptor(
        evaluation_purpose=PURPOSE_UNSPECIFIED,
        input_origin=ORIGIN_EXTERNALLY_SUPPLIED,
        delivery_path=DELIVERY_UNKNOWN,
    )
    assessment = assess_source_evidence(unspecified)
    assert assessment.admission_authority == SUPPORT_UNAVAILABLE
    assert assessment.original_historical_availability == SUPPORT_UNAVAILABLE
    assert "invocation_purpose_unspecified" in assessment.limitation_codes
    assert SetupLifecycleState.TRIGGERED not in {SetupLifecycleState.REJECTED}
    assert EVIDENCE_SOURCE_ONLY
