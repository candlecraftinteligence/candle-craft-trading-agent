"""Deterministic integrity diagnostics. Synthetic cases are labeled synthetic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from app.data.candle_integrity import (
    CandleIntegrityError,
    closed_candles_as_of,
    normalize_utc_timestamp,
    validate_candle_sequence,
)
from app.data.dtos import NA
from app.data.normalizers.binance import normalize_binance_open_interest
from app.data.normalizers.bybit import normalize_bybit_open_interest, normalize_bybit_ticker
from app.data.timeframes import resample_ohlcv_candles
from app.microstructure.liquidation import parse_binance_liquidation
from app.microstructure.order_book import parse_binance_depth_snapshot

from research.market_data_foundation.timeutil import (
    classify_unit_confusion,
    normalize_explicit,
    originally_available,
)


@dataclass(frozen=True)
class Finding:
    finding_id: str
    family: str
    severity: str
    classification: str
    summary: str
    expected: str
    observed: str
    consumers: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    follow_up: str
    reproduction: str
    production_incident: str = "not established"

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "family": self.family,
            "severity": self.severity,
            "classification": self.classification,
            "summary": self.summary,
            "expected": self.expected,
            "observed": self.observed,
            "consumers": list(self.consumers),
            "evidence_ids": list(self.evidence_ids),
            "follow_up": self.follow_up,
            "reproduction": self.reproduction,
            "production_incident": self.production_incident,
        }


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def ohlc_problems(candle: dict[str, Any]) -> tuple[str, ...]:
    """Semantic OHLC and volume checks. Zero volume on a present bar is not a gap."""

    problems: list[str] = []
    prices = [candle.get(name) for name in ("open", "high", "low", "close")]
    parsed = [_decimal(item) for item in prices]
    if any(item is None for item in parsed):
        problems.append("non_finite_or_missing_price")
        return tuple(problems)
    open_price, high, low, close = parsed
    assert open_price is not None and high is not None and low is not None and close is not None
    if high < low or high < max(open_price, close) or low > min(open_price, close):
        problems.append("impossible_ohlc")
    volume = candle.get("volume")
    if volume is None:
        problems.append("volume_missing")
    else:
        parsed_volume = _decimal(volume)
        if parsed_volume is None:
            problems.append("volume_non_finite")
        elif parsed_volume < 0:
            problems.append("volume_negative")
        elif parsed_volume == 0 and open_price == high == low == close:
            problems.append("present_no_trade")
    return tuple(problems)


def diagnose_sequence(candles: Sequence[dict[str, Any]], *, timeframe: str) -> list[str]:
    reasons: list[str] = []
    try:
        validate_candle_sequence(candles, timeframe=timeframe)
    except CandleIntegrityError as exc:
        reasons.append(exc.reason.value)
    for index, candle in enumerate(candles):
        for problem in ohlc_problems(candle):
            reasons.append(f"{problem}[{index}]")
    return reasons


def closed_bar_status(
    candles: Sequence[dict[str, Any]],
    *,
    timeframe: str,
    decision_at: datetime,
) -> dict[str, Any]:
    window = closed_candles_as_of(
        candles,
        timeframe=timeframe,
        decision_timestamp=decision_at,
        minimum_closed_history=0,
    )
    return {
        "closed_count": len(window.timeline),
        "excluded_unclosed_count": window.excluded_unclosed_count,
    }


def production_integer_timestamp_is_milliseconds(value: int) -> datetime:
    """Production ``normalize_utc_timestamp`` treats every integer as milliseconds."""

    return normalize_utc_timestamp(value, field_name="research_probe")


def resample_2d_checked(
    daily: Sequence[dict[str, Any]],
    *,
    decision_at: datetime,
) -> dict[str, Any]:
    produced = resample_ohlcv_candles(daily, "2d", decision_timestamp=decision_at)
    rows = []
    for item in produced:
        volume = Decimal(str(item["volume"]))
        rows.append(
            {
                "timestamp": int(item["timestamp"]),
                "open": str(item["open"]),
                "high": str(item["high"]),
                "low": str(item["low"]),
                "close": str(item["close"]),
                "volume": str(volume),
            }
        )
    return {"emitted": len(rows), "rows": rows, "target": "2d"}


def unsupported_resample(target: str) -> str:
    try:
        resample_ohlcv_candles((), target, decision_timestamp=datetime(2026, 1, 3, tzinfo=UTC))
    except ValueError as exc:
        return str(exc)
    return "accepted"


def linear_position_versus_notional(
    *,
    base_quantity: Decimal,
    mark_before: Decimal,
    mark_after: Decimal,
) -> dict[str, str]:
    """Quote notional can change while the base position does not."""

    before = base_quantity * mark_before
    after = base_quantity * mark_after
    return {
        "base_quantity": str(base_quantity),
        "notional_before_usdt": str(before),
        "notional_after_usdt": str(after),
        "base_position_change": "0",
        "notional_change_usdt": str(after - before),
        "position_growth": "false",
        "quote_unit": "USDT",
        "usd_conversion": "not_applied",
    }


def inverse_contract_versus_coin(
    *,
    contracts: Decimal,
    contract_face_usd: Decimal,
    price_before: Decimal,
    price_after: Decimal,
) -> dict[str, str]:
    """Coin exposure of an inverse contract changes with price at constant contracts."""

    coin_before = contracts * contract_face_usd / price_before
    coin_after = contracts * contract_face_usd / price_after
    return {
        "contracts": str(contracts),
        "contract_face_usd": str(contract_face_usd),
        "coin_before": str(coin_before),
        "coin_after": str(coin_after),
        "contract_change": "0",
        "position_growth_in_contracts": "false",
    }


def bybit_open_interest_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Keep both Bybit fields. Do not divide either value by two."""

    open_interest = row.get("openInterest")
    single = row.get("singleOpenInterest")
    payload = {
        "retCode": 0,
        "result": {
            "list": [
                {
                    "openInterest": open_interest,
                    "timestamp": row.get("timestamp", "1700000000000"),
                    **({"singleOpenInterest": single} if single is not None else {}),
                }
            ]
        },
    }
    dto = normalize_bybit_open_interest("BTCUSDT", payload)
    differ = (
        open_interest is not None
        and single is not None
        and Decimal(str(open_interest)) != Decimal(str(single))
    )
    return {
        "repository_open_interest": str(dto.open_interest),
        "repository_unit": dto.unit,
        "single_open_interest": None if single is None else str(single),
        "fields_differ": differ,
        "divided_by_two": False,
        "disposition": "unverified" if differ or dto.unit == NA else "verified",
        "reason": "counting_convention_not_proven" if differ or dto.unit == NA else "single_field",
    }


def binance_current_open_interest_unit(payload: dict[str, Any]) -> str:
    dto = normalize_binance_open_interest("BTCUSDT", payload)
    return str(dto.unit)


def funding_fraction_percent(rate: Decimal) -> dict[str, str]:
    disposition = "incompatible" if abs(rate) >= 1 else "verified"
    return {
        "native_fraction": str(rate),
        "percent": str(rate * Decimal("100")),
        "basis_points": str(rate * Decimal("10000")),
        "disposition": disposition,
        "auto_divided": "false",
    }


def bybit_ticker_timestamp_source(envelope_time_ms: int) -> dict[str, Any]:
    payload = {
        "retCode": 0,
        "time": envelope_time_ms,
        "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100"}]},
    }
    dto = normalize_bybit_ticker("BTCUSDT", payload)
    return {
        "dto_timestamp": dto.timestamp,
        "envelope_time_ms": envelope_time_ms,
        "measurement_timestamp": None,
        "copied_envelope_time": dto.timestamp == envelope_time_ms,
    }


def order_book_snapshot_diagnosis(payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = parse_binance_depth_snapshot(payload)
    best_bid = snapshot.bids[0].price if snapshot.bids else None
    best_ask = snapshot.asks[0].price if snapshot.asks else None
    crossed = best_bid is not None and best_ask is not None and best_bid >= best_ask
    zero_deleted = any(level.quantity == 0 for level in (*snapshot.bids, *snapshot.asks))
    return {
        "last_update_id": snapshot.last_update_id,
        "synchronization": "not_applicable",
        "synchronization_reason": "snapshot_without_diff_stream",
        "crossed": crossed,
        "zero_quantity_level": zero_deleted,
        "event_time_present": snapshot.event_time is not None,
        "transaction_time_present": snapshot.transaction_time is not None,
    }


def liquidation_observation(payload: dict[str, Any] | None, *, coverage: str) -> dict[str, Any]:
    if payload is None:
        return {
            "disposition": "missing",
            "cci_mark": "N/A",
            "event_count": None,
            "not_zero": True,
            "coverage": coverage,
            "side": None,
        }
    order = parse_binance_liquidation(payload)
    return {
        "disposition": "verified" if coverage == "observed_event" else "unverified",
        "cci_mark": "verified" if coverage == "observed_event" else "Unverified",
        "event_count": 1,
        "not_zero": True,
        "coverage": coverage,
        "side": order.liquidated_position_side.value,
        "order_side": order.order_side,
        "quote_notional": str(order.quote_notional),
    }


def population_label(metric_id: str) -> str:
    labels = {
        "binance_global_long_short_account_ratio": "all_accounts_long_short_ratio",
        "bybit_account_ratio": "all_accounts_long_short_ratio",
        "top_trader_position_ratio": "top_trader_position_ratio",
        "taker_flow": "aggressor_flow",
    }
    return labels.get(metric_id, "unknown_population")


def populations_are_same_metric(left: str, right: str) -> bool:
    return population_label(left) == population_label(right) and population_label(left) != "unknown_population"


def canonical_findings() -> tuple[Finding, ...]:
    """Reproducible code-contract findings. None of these is a production incident claim."""

    unit = classify_unit_confusion(1_700_000_000, interval=timedelta(minutes=1), sibling_delta=60)
    cutoff = datetime(2026, 1, 2, tzinfo=UTC)
    leakage = originally_available(
        measurement_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
        available_to_cci_at=datetime(2026, 1, 3, tzinfo=UTC),
        decision_cutoff=cutoff,
    )
    bybit_oi = bybit_open_interest_fields(
        {"openInterest": "20", "singleOpenInterest": "10", "timestamp": "1700000000000"}
    )
    ticker = bybit_ticker_timestamp_source(1_700_000_000_000)
    book = order_book_snapshot_diagnosis(
        {
            "lastUpdateId": 10,
            "E": 1_700_000_000_000,
            "T": 1_700_000_000_000,
            "bids": [["100", "1"]],
            "asks": [["99", "1"]],
        }
    )
    empty_liq = liquidation_observation(None, coverage="no_events_received")
    return (
        Finding(
            finding_id="MDF-TIME-001",
            family="temporal",
            severity="medium",
            classification="code_contract",
            summary="Production integer timestamps are milliseconds, and a seconds spacing is not rewritten.",
            expected="Callers declare seconds or milliseconds. Acquisition time is not a substitute producer time.",
            observed=(
                f"sibling spacing classification={unit}; historical availability of a bar fetched "
                f"after the cutoff is {leakage}"
            ),
            consumers=("app.data.candle_integrity.normalize_utc_timestamp", "freshness and replay cutoffs"),
            evidence_ids=("synthetic:seconds-spacing", "synthetic:late-fetch"),
            follow_up="Keep second-based provider timestamps off the millisecond normalizer.",
            reproduction="timeutil.classify_unit_confusion and originally_available",
        ),
        Finding(
            finding_id="MDF-CANDLE-001",
            family="candles",
            severity="medium",
            classification="code_contract",
            summary="Shared candle-sequence validation does not reject impossible OHLC or negative volume.",
            expected="A shared integrity boundary rejects impossible prices and negative volume before features.",
            observed="validate_candle_sequence checks order, anchors, and continuity. OHLC and volume checks exist later in selected consumers.",
            consumers=(
                "app.data.candle_integrity.validate_candle_sequence",
                "app.analytics.target_intelligence",
                "app.agents.technical_structure",
            ),
            evidence_ids=("code:candle_integrity.py",),
            follow_up="A later phase can fail closed at the shared boundary without weakening freshness or RR.",
            reproduction="ohlc_problems on high<low and volume<0; validate_candle_sequence does not raise for those fields",
        ),
        Finding(
            finding_id="MDF-OI-001",
            family="open_interest",
            severity="medium",
            classification="code_contract",
            summary="Bybit openInterest is stored without a unit label. A differing singleOpenInterest is not halved.",
            expected="The DTO records both-sides versus single-side and the unit. Differing fields are not silently averaged.",
            observed=(
                f"repository value {bybit_oi['repository_open_interest']} unit {bybit_oi['repository_unit']}; "
                f"divided_by_two={bybit_oi['divided_by_two']}; disposition={bybit_oi['disposition']}. "
                "Bybit's public open-interest page retrieved 2026-10-19 defines openInterest as the sum of both sides "
                "and singleOpenInterest as one side. For linear BTCUSDT the documented unit is BTC."
            ),
            consumers=("app.data.normalizers.bybit.normalize_bybit_open_interest", "derivatives enrichment"),
            evidence_ids=("synthetic:bybit-oi-both-fields",),
            follow_up="Record the applicable Bybit schema field and unit before any cross-venue OI comparison.",
            reproduction="bybit_open_interest_fields",
        ),
        Finding(
            finding_id="MDF-PX-001",
            family="prices",
            severity="low",
            classification="code_contract",
            summary="Bybit ticker DTO timestamp copies the response envelope time when no measurement time exists.",
            expected="Envelope time and measurement time stay separate. A missing measurement time stays missing.",
            observed=f"copied_envelope_time={ticker['copied_envelope_time']} measurement_timestamp=None",
            consumers=("app.data.normalizers.bybit.normalize_bybit_ticker",),
            evidence_ids=("synthetic:bybit-ticker-envelope",),
            follow_up="Store response time separately and leave measurement time N/A when the payload has none.",
            reproduction="bybit_ticker_timestamp_source",
        ),
        Finding(
            finding_id="MDF-BOOK-001",
            family="order_book",
            severity="info",
            classification="methodology",
            summary="A REST snapshot alone cannot prove diff-stream synchronization.",
            expected="Synchronization is not_applicable without a sequenced diff stream.",
            observed=(
                f"synchronization={book['synchronization']} crossed={book['crossed']} "
                "on a synthetic bid 100 / ask 99 snapshot"
            ),
            consumers=("app.microstructure.order_book.parse_binance_depth_snapshot",),
            evidence_ids=("synthetic:crossed-snapshot",),
            follow_up="Keep order-book research on the existing snapshot-plus-diff implementation.",
            reproduction="order_book_snapshot_diagnosis",
        ),
        Finding(
            finding_id="MDF-LIQ-001",
            family="liquidations",
            severity="high",
            classification="code_contract",
            summary="No received liquidation events is missing coverage, not zero liquidations.",
            expected="Empty or absent streams stay N/A unless coverage proves there were no events.",
            observed=f"disposition={empty_liq['disposition']} event_count={empty_liq['event_count']}",
            consumers=("app.microstructure.liquidation_service",),
            evidence_ids=("synthetic:empty-liquidation-stream",),
            follow_up="Do not let an unsubscribed or gapped stream satisfy a liquidation feature.",
            reproduction="liquidation_observation(None)",
        ),
        Finding(
            finding_id="MDF-FUND-001",
            family="funding",
            severity="low",
            classification="code_contract",
            summary="CCI funding reads are last settled history, not the predicted premium-index rate.",
            expected="Settled and predicted rates stay distinct.",
            observed="BinanceFuturesClient.get_funding_rate uses /fapi/v1/fundingRate history. premiumIndex is not requested.",
            consumers=("app.analytics.derivatives_enrichment",),
            evidence_ids=("code:binance_futures.py:get_funding_rate",),
            follow_up="Name the consumer value last settled funding unless a predicted endpoint is added and tested.",
            reproduction="inspect BinanceFuturesClient.get_funding_rate",
        ),
    )
