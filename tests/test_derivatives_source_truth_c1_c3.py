"""Acceptance coverage for duplicate semantics, share precision, and lineage bounds.

Provider-shaped fixtures use the public describe/parse entry points. The final
section builds QuantityFact values that no exchange payload would emit.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.analytics.derivatives_source_truth import (
    DerivativesSourceTruthResult,
    evaluate_derivatives_source_truth,
    native_position_change_pct,
    write_dev_shadow_artifact,
)
from app.data.derivatives_instrument import parse_bybit_linear_instrument
from app.data.derivatives_source_evidence import (
    COUNT_NOT_APPLICABLE,
    QUALITY_UNVERIFIED,
    RATE_SETTLED_FUNDING,
    UNIT_ACCOUNT_RATIO,
    _account_ratio_quantity,
    describe_binance_account_ratio,
    describe_binance_funding_history,
    describe_binance_open_interest,
    describe_binance_open_interest_history,
    describe_bybit_account_ratio,
    describe_bybit_funding_history,
    describe_bybit_open_interest,
    parse_quantity,
)
from app.data.dtos import NA
from app.pipeline.scanner_runner import ScannerRunConfig, ScannerSymbolResult
from app.storage.database import SCHEMA_VERSION, connect_database, identify_schema_version, initialize_database
from tests.test_derivatives_source_truth import (
    CUTOFF,
    FUNDING_AT,
    PREVIOUS,
    acquired,
    assert_cannot_confirm,
    binance_instrument,
    funding_payload,
    millis,
    oi_history_payload,
    ratio_payload,
)

EIGHT_HOURS_MS = 8 * 60 * 60 * 1000


def fund_row(symbol: str = "BTCUSDT", rate: str = "0.0001", moment=FUNDING_AT, **extra):
    row = {"symbol": symbol, "fundingRate": rate, "fundingRateTimestamp": millis(moment)}
    row.update(extra)
    return row


def ratio_row(symbol: str = "BTCUSDT", buy: str = "0.8", sell: str = "0.2", moment=CUTOFF):
    return {"symbol": symbol, "buyRatio": buy, "sellRatio": sell, "timestamp": millis(moment)}


def bybit_instrument():
    return parse_bybit_linear_instrument(
        {
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "LinearPerpetual",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "deliveryTime": "0",
                        "fundingInterval": "480",
                    }
                ],
            }
        },
        requested_symbol="BTCUSDT",
        acquisition=acquired(),
    )


def bybit_open_interest(rows: list[tuple] | None = None):
    if rows is None:
        rows = [(PREVIOUS, "100"), (CUTOFF, "110")]
    return describe_bybit_open_interest(
        requested_symbol="BTCUSDT",
        payload={
            "time": millis(CUTOFF),
            "result": {
                "category": "linear",
                "symbol": "BTCUSDT",
                "list": [
                    {"openInterest": quantity, "timestamp": millis(moment)}
                    for moment, quantity in rows
                ],
            },
        },
        request_params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min"},
        capture_clock=None,
        acquisition=acquired(),
    )


def bybit_funding(rows: list[dict]):
    return describe_bybit_funding_history(
        requested_symbol="BTCUSDT",
        payload={"time": millis(CUTOFF), "result": {"category": "linear", "list": rows}},
        request_params={"category": "linear", "symbol": "BTCUSDT", "limit": len(rows)},
        capture_clock=None,
        acquisition=acquired(),
    )


def bybit_ratio(rows: list[dict]):
    return describe_bybit_account_ratio(
        requested_symbol="BTCUSDT",
        payload={"time": millis(CUTOFF), "result": {"category": "linear", "list": rows}},
        request_params={"category": "linear", "symbol": "BTCUSDT", "period": "5min", "limit": len(rows)},
        capture_clock=None,
        acquisition=acquired(),
    )


def evaluate_bybit(*, funding_rows=None, ratio_rows=None, oi_rows=None, funding_history=None, funding=None, account_ratio=None):
    if funding_rows is None:
        funding_rows = [fund_row()]
    return evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=bybit_instrument(),
        open_interest_history=bybit_open_interest(oi_rows),
        funding_history=bybit_funding(funding_rows) if funding_history is None else funding_history,
        funding=funding,
        account_ratio=bybit_ratio(ratio_rows) if ratio_rows is not None else account_ratio,
    )


def outcome(result):
    return (
        result.classification,
        result.funding_label,
        result.oi_change_pct,
        result.account_ratio,
        result.supports_long,
        result.supports_short,
    )


def binance_funding(rows: list[dict]):
    return describe_binance_funding_history(
        requested_symbol="BTCUSDT",
        payload=rows,
        request_params={"symbol": "BTCUSDT", "limit": len(rows)},
        capture_clock=None,
        acquisition=acquired(),
    )


def evaluate_binance_funding(rows: list[dict]):
    """Current-context fixture. The 8h interval is supplied by the test, not exchangeInfo."""

    instrument = replace(binance_instrument(), funding_interval_ms=EIGHT_HOURS_MS)
    acquisition = acquired()
    return evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=instrument,
        open_interest=describe_binance_open_interest(
            requested_symbol="BTCUSDT",
            payload={"symbol": "BTCUSDT", "openInterest": "110", "time": millis(CUTOFF)},
            request_params={"symbol": "BTCUSDT"},
            capture_clock=None,
            acquisition=acquisition,
        ),
        open_interest_history=describe_binance_open_interest_history(
            requested_symbol="BTCUSDT",
            payload=oi_history_payload([(PREVIOUS, "100", None), (CUTOFF, "110", None)]),
            request_params={"symbol": "BTCUSDT", "period": "5m", "limit": 30},
            capture_clock=None,
            acquisition=acquisition,
        ),
        funding_history=binance_funding(rows),
    )


def roles(result) -> set[tuple[str, str]]:
    return {(item["role"], item["raw_value"]) for item in result.to_dict()["lineage"]}


def test_bybit_control_is_actually_eligible() -> None:
    result = evaluate_bybit()
    assert_cannot_confirm(result)
    assert result.classification == "eligible_no_conflict_detected"
    assert result.oi_change_pct == "10.00000000"
    assert result.funding_label == RATE_SETTLED_FUNDING
    assert result.funding_rate == "0.00010000"
    assert result.account_ratio == NA
    assert result.supports_long == "eligible_no_conflict_detected"
    assert result.funding_cadence_ms == 480 * 60 * 1000


def test_funding_subtype_duplicates_cannot_choose_eligibility_by_order() -> None:
    ordinary = fund_row()
    unknown = fund_row(rateType="unexpected")
    control = evaluate_bybit()
    good_first = evaluate_bybit(funding_rows=[ordinary, unknown])
    bad_first = evaluate_bybit(funding_rows=[unknown, ordinary])
    assert outcome(good_first) == outcome(bad_first)
    assert good_first.classification != "eligible_no_conflict_detected"
    assert good_first.funding_label != RATE_SETTLED_FUNDING
    assert "unexpected_funding_subtype" in good_first.reason_codes
    assert "unexpected_funding_subtype" in bad_first.reason_codes
    assert good_first.oi_change_pct == control.oi_change_pct
    assert_cannot_confirm(good_first)
    assert_cannot_confirm(bad_first)
    unexpected = next(item for item in bad_first.to_dict()["lineage"] if item.get("rate_kind") == "unexpected")
    assert unexpected["role"] == "excluded_funding"
    assert "unexpected_funding_subtype" in unexpected["reason_codes"]


def test_equal_funding_identity_duplicates_keep_the_requested_instrument() -> None:
    control = evaluate_bybit()
    btc = fund_row()
    eth = fund_row(symbol="ETHUSDT")
    btc_first = evaluate_bybit(funding_rows=[btc, eth])
    eth_first = evaluate_bybit(funding_rows=[eth, btc])
    assert outcome(btc_first) == outcome(eth_first) == outcome(control)
    assert_cannot_confirm(btc_first)


def test_foreign_funding_subtype_does_not_veto_the_requested_row() -> None:
    control = evaluate_bybit()
    local = fund_row()
    foreign = fund_row(symbol="ETHUSDT", rateType="unexpected")
    forward = evaluate_bybit(funding_rows=[local, foreign])
    reverse = evaluate_bybit(funding_rows=[foreign, local])
    assert outcome(forward) == outcome(reverse) == outcome(control)


def test_foreign_venue_funding_does_not_veto_in_either_batch_order() -> None:
    control = evaluate_bybit()
    local = bybit_funding([fund_row()])
    foreign = binance_funding(funding_payload("0.0001", FUNDING_AT))
    history_first = evaluate_bybit(funding_history=local, funding=foreign)
    current_first = evaluate_bybit(funding_history=foreign, funding=local)
    assert outcome(history_first) == outcome(current_first) == outcome(control)


def test_conflicting_funding_values_are_order_independent() -> None:
    low = fund_row(rate="0.0001")
    high = fund_row(rate="0.0002")
    forward = evaluate_bybit(funding_rows=[low, high])
    reverse = evaluate_bybit(funding_rows=[high, low])
    assert outcome(forward) == outcome(reverse)
    assert forward.classification != "eligible_no_conflict_detected"
    assert "duplicate_conflict" in forward.reason_codes
    assert forward.funding_label != RATE_SETTLED_FUNDING


def test_identical_valid_funding_duplicates_still_collapse() -> None:
    control = evaluate_bybit()
    duplicated = evaluate_bybit(funding_rows=[fund_row(), fund_row()])
    assert outcome(duplicated) == outcome(control)
    selected = [item for item in duplicated.to_dict()["lineage"] if item["role"] == "selected_funding"]
    assert len(selected) == 1
    assert selected[0]["raw_value"] == "0.0001"
    assert selected[0]["rate_kind"] == RATE_SETTLED_FUNDING


def test_identical_funding_across_current_and_history_collapses() -> None:
    control = evaluate_bybit()
    batch = bybit_funding([fund_row()])
    combined = evaluate_bybit(funding_history=batch, funding=batch)
    assert outcome(combined) == outcome(control)


def test_delivery_defect_blocks_either_funding_batch_order() -> None:
    good = bybit_funding([fund_row()])
    bad = replace(
        bybit_funding([fund_row()]),
        cache=replace(good.cache, delivered_at=CUTOFF - timedelta(days=1), original_acquired_at=CUTOFF),
    )
    history_clean = evaluate_bybit(funding_history=good, funding=bad)
    history_bad = evaluate_bybit(funding_history=bad, funding=good)
    assert outcome(history_clean) == outcome(history_bad)
    assert history_clean.classification != "eligible_no_conflict_detected"
    assert "delivery_before_acquisition" in history_clean.reason_codes


def test_response_after_acquisition_sibling_blocks_either_order() -> None:
    batch = bybit_funding([fund_row(), fund_row()])
    late = batch.observations[1].response_time
    poisoned = replace(
        batch,
        observations=(
            batch.observations[0],
            replace(batch.observations[1], response_time=replace(late, at=CUTOFF + timedelta(days=1))),
        ),
    )
    forward = evaluate_bybit(funding_history=poisoned)
    flipped = replace(poisoned, observations=(poisoned.observations[1], poisoned.observations[0]))
    reverse = evaluate_bybit(funding_history=flipped)
    assert outcome(forward) == outcome(reverse)
    assert forward.classification != "eligible_no_conflict_detected"
    assert "response_after_acquisition" in forward.reason_codes or "conflicting_provenance" in forward.reason_codes


def test_malformed_funding_timestamp_does_not_invent_time_or_veto() -> None:
    control = evaluate_bybit()
    broken = fund_row()
    broken["fundingRateTimestamp"] = "not-a-timestamp"
    forward = evaluate_bybit(funding_rows=[fund_row(), broken])
    reverse = evaluate_bybit(funding_rows=[broken, fund_row()])
    assert outcome(forward) == outcome(reverse) == outcome(control)
    encoded = json.dumps(forward.to_dict())
    assert "1970-01-01" not in encoded
    assert any(item["metric_time"] is None and item["raw_value"] == "0.0001" for item in forward.to_dict()["lineage"])


def test_duplicate_ratio_identity_is_order_independent() -> None:
    control = evaluate_bybit()
    ratio_only = evaluate_bybit(ratio_rows=[ratio_row()])
    assert ratio_only.classification == "conflict_detected"
    assert ratio_only.account_ratio == "4.00000000"
    assert ratio_only.supports_long == "false"
    assert ratio_only.oi_change_pct == control.oi_change_pct
    btc = ratio_row()
    eth = ratio_row(symbol="ETHUSDT")
    btc_first = evaluate_bybit(ratio_rows=[btc, eth])
    eth_first = evaluate_bybit(ratio_rows=[eth, btc])
    assert outcome(btc_first) == outcome(eth_first) == outcome(ratio_only)
    shares = next(item for item in btc_first.to_dict()["lineage"] if item["role"] == "selected_account_ratio")
    assert shares["raw_field"] == "buyRatio/sellRatio"
    assert ("buyRatio", "0.8") in {(part["raw_field"], part["raw_value"]) for part in shares["components"]}
    assert ("sellRatio", "0.2") in {(part["raw_field"], part["raw_value"]) for part in shares["components"]}


def test_conflicting_local_ratios_do_not_manufacture_a_side() -> None:
    control = evaluate_bybit()
    forward = evaluate_bybit(ratio_rows=[ratio_row(buy="0.8", sell="0.2"), ratio_row(buy="0.6", sell="0.4")])
    reverse = evaluate_bybit(ratio_rows=[ratio_row(buy="0.6", sell="0.4"), ratio_row(buy="0.8", sell="0.2")])
    assert outcome(forward) == outcome(reverse)
    assert forward.account_ratio == NA
    assert forward.classification == control.classification
    assert forward.supports_long == control.supports_long
    assert "duplicate_conflict" in forward.reason_codes


def test_invalid_local_ratio_does_not_hide_a_valid_sibling() -> None:
    valid = evaluate_bybit(ratio_rows=[ratio_row()])
    forward = evaluate_bybit(ratio_rows=[ratio_row(), ratio_row(buy="2", sell="0.5")])
    reverse = evaluate_bybit(ratio_rows=[ratio_row(buy="2", sell="0.5"), ratio_row()])
    assert outcome(forward) == outcome(reverse) == outcome(valid)


def test_optional_ratio_cutoff_does_not_reclassify_funding_and_oi() -> None:
    control = evaluate_bybit()
    late = describe_bybit_account_ratio(
        requested_symbol="BTCUSDT",
        payload={
            "time": millis(CUTOFF),
            "result": {"category": "linear", "list": [ratio_row()]},
        },
        request_params={"category": "linear", "symbol": "BTCUSDT", "period": "5min"},
        capture_clock=None,
        acquisition=acquired(CUTOFF + timedelta(seconds=1)),
    )
    result = evaluate_bybit(account_ratio=late)
    assert outcome(result) == outcome(control)
    assert "acquisition_after_cutoff" not in result.reason_codes
    future = bybit_ratio([ratio_row(moment=CUTOFF + timedelta(minutes=5))])
    future_result = evaluate_bybit(account_ratio=future)
    assert outcome(future_result) == outcome(control)
    assert "metric_after_cutoff" not in future_result.reason_codes


def test_coarse_number_format_does_not_validate_an_impossible_holder_partition() -> None:
    control = evaluate_bybit()
    coarse = evaluate_bybit(ratio_rows=[ratio_row(buy="1", sell="0.2")])
    padded = evaluate_bybit(ratio_rows=[ratio_row(buy="1.0000", sell="0.2000")])
    zero_coarse = evaluate_bybit(ratio_rows=[ratio_row(buy="0", sell="0.2")])
    zero_padded = evaluate_bybit(ratio_rows=[ratio_row(buy="0.0000", sell="0.2000")])
    assert coarse.account_ratio == NA
    assert padded.account_ratio == NA
    assert zero_coarse.account_ratio == NA
    assert zero_padded.account_ratio == NA
    for result in (coarse, padded, zero_coarse, zero_padded):
        assert result.classification == control.classification
        assert result.supports_long == control.supports_long
        assert "account_share_inconsistent" in result.reason_codes
        assert_cannot_confirm(result)
    valid = evaluate_bybit(ratio_rows=[ratio_row(buy="0.8", sell="0.2")])
    also_valid = evaluate_bybit(ratio_rows=[ratio_row(buy="0.80", sell="0.20")])
    assert valid.account_ratio == also_valid.account_ratio == "4.00000000"


def test_explicit_ratio_precision_follows_component_uncertainty() -> None:
    consistent = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=ratio_payload(CUTOFF, ratio="99.4016", long_account="0.9900", short_account="0.0100"),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    assert consistent.observations[0].semantic_quality == "verified"
    consistent_result = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF, account_ratio=consistent)
    assert consistent_result.account_ratio == "99.40160000"
    assert "ratio_component_conflict" not in consistent_result.reason_codes

    documented = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=ratio_payload(CUTOFF, ratio="1.8105", long_account="0.6442", short_account="0.3558"),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    documented_result = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF, account_ratio=documented)
    assert documented_result.account_ratio == "1.81050000"
    assert "ratio_component_conflict" not in documented_result.reason_codes

    exact = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=ratio_payload(CUTOFF, ratio="4", long_account="0.8", short_account="0.2"),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    exact_result = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF, account_ratio=exact)
    assert exact_result.account_ratio == "4.00000000"

    outside = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=ratio_payload(CUTOFF, ratio="9", long_account="0.8", short_account="0.2"),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    outside_result = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF, account_ratio=outside)
    assert outside_result.account_ratio == NA
    assert "ratio_component_conflict" in outside_result.reason_codes

    published_conflict = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=ratio_payload(CUTOFF, ratio="9", long_account="0.7", short_account="0.3"),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    published = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF, account_ratio=published_conflict)
    assert published.account_ratio == NA
    assert "ratio_component_conflict" in published.reason_codes


def test_zero_near_zero_and_non_finite_populations_stay_unused() -> None:
    control = evaluate_bybit()
    zero_short = evaluate_bybit(ratio_rows=[ratio_row(buy="0.8", sell="0")])
    near_zero = evaluate_bybit(ratio_rows=[ratio_row(buy="1.0000", sell="0.0000")])
    malformed = evaluate_bybit(ratio_rows=[ratio_row(buy="abc", sell="0.2")])
    non_finite = evaluate_bybit(ratio_rows=[ratio_row(buy="NaN", sell="0.2")])
    infinite = evaluate_bybit(ratio_rows=[ratio_row(buy="Infinity", sell="0.2")])
    for result in (zero_short, near_zero, malformed, non_finite, infinite):
        assert result.account_ratio == NA
        assert result.classification == control.classification
        assert result.supports_long == control.supports_long
    assert "invalid_denominator" in zero_short.reason_codes
    assert "invalid_denominator" in near_zero.reason_codes
    assert "non_finite_quantity" in non_finite.reason_codes
    assert "non_finite_quantity" in infinite.reason_codes

    justified = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=ratio_payload(CUTOFF, ratio="9999", long_account="0.9999", short_account="0.0001"),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    justified_result = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF, account_ratio=justified)
    assert justified_result.account_ratio == "9999.00000000"
    assert "account_share_precision_unjustified" not in justified_result.reason_codes
    assert "ratio_component_conflict" not in justified_result.reason_codes


def test_regular_funding_is_settled_and_special_is_not_relabeled() -> None:
    regular_rows = funding_payload("0.0001", FUNDING_AT)
    regular_rows[0]["rateType"] = "Regular"
    regular = binance_funding(regular_rows)
    assert regular.observations[0].rate_kind == RATE_SETTLED_FUNDING
    assert regular.observations[0].subtype_declarations == (("rateType", "Regular"),)
    assert "regular_funding" in regular.observations[0].reason_codes
    assert "unexpected_funding_subtype" not in regular.observations[0].reason_codes
    regular_result = evaluate_binance_funding(regular_rows)
    assert regular_result.classification == "eligible_no_conflict_detected"
    assert regular_result.funding_label == RATE_SETTLED_FUNDING
    regular_fact = next(item for item in regular_result.to_dict()["lineage"] if item["role"] == "selected_funding")
    assert regular_fact["components"] == [{"raw_field": "rateType", "raw_value": "Regular"}]
    assert_cannot_confirm(regular_result)

    alternate = funding_payload("0.0001", FUNDING_AT)
    alternate[0]["fundingRateType"] = "regular"
    alternate_batch = binance_funding(alternate)
    assert alternate_batch.observations[0].semantic_quality == QUALITY_UNVERIFIED
    assert alternate_batch.observations[0].rate_kind == "regular"
    assert alternate_batch.observations[0].subtype_declarations == (("fundingRateType", "regular"),)
    assert "unexpected_funding_subtype" in alternate_batch.observations[0].reason_codes
    alternate_result = evaluate_binance_funding(alternate)
    assert alternate_result.funding_label != RATE_SETTLED_FUNDING
    assert alternate_result.classification == "unverified"
    assert "unexpected_funding_subtype" in alternate_result.reason_codes
    assert_cannot_confirm(alternate_result)

    special_rows = funding_payload("0.0001", FUNDING_AT)
    special_rows[0]["rateType"] = "Special"
    special = binance_funding(special_rows)
    assert special.observations[0].rate_kind == "Special"
    assert special.observations[0].rate_kind != RATE_SETTLED_FUNDING
    assert "unsupported_funding_subtype" in special.observations[0].reason_codes
    special_result = evaluate_binance_funding(special_rows)
    assert special_result.classification != "eligible_no_conflict_detected"
    assert special_result.funding_label != RATE_SETTLED_FUNDING
    assert "unsupported_funding_subtype" in special_result.reason_codes
    special_fact = next(item for item in special_result.to_dict()["lineage"] if item.get("rate_kind") == "Special")
    assert special_fact["role"] == "excluded_funding"

    ordinary = funding_payload("0.0001", FUNDING_AT)[0]
    special_row = {**ordinary, "rateType": "Special"}
    special_first = evaluate_binance_funding([special_row, ordinary])
    ordinary_first = evaluate_binance_funding([ordinary, special_row])
    assert outcome(special_first) == outcome(ordinary_first)
    assert special_first.funding_label != RATE_SETTLED_FUNDING
    assert "unsupported_funding_subtype" in special_first.reason_codes

    settled_token = funding_payload("0.0001", FUNDING_AT)
    settled_token[0]["rateType"] = "settled"
    labeled = binance_funding(settled_token)
    assert labeled.observations[0].rate_kind == "settled"
    assert labeled.observations[0].rate_kind != RATE_SETTLED_FUNDING
    assert "unexpected_funding_subtype" in labeled.observations[0].reason_codes

    absent = binance_funding(funding_payload("0.0001", FUNDING_AT))
    assert absent.observations[0].rate_kind == RATE_SETTLED_FUNDING
    vendor = parse_bybit_linear_instrument(
        {
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "LinearPerpetual",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "deliveryTime": "0",
                        "fundingInterval": "480",
                    }
                ],
            }
        },
        requested_symbol="BTCUSDT",
        acquisition=acquired(),
    )
    untouched = binance_instrument()
    assert untouched.funding_interval_ms is None
    assert vendor.funding_interval_ms == 480 * 60 * 1000


def test_two_row_oi_keeps_current_and_predecessor_distinct() -> None:
    result = evaluate_bybit()
    assert ("selected_current_oi", "110") in roles(result)
    assert ("selected_previous_oi", "100") in roles(result)
    assert ("selected_current_oi", "100") not in roles(result)
    assert sum(1 for item in result.to_dict()["lineage"] if item["role"] == "selected_current_oi") == 1


def test_default_history_retains_the_evidence_used_in_the_decision() -> None:
    funding_rows = [fund_row(moment=FUNDING_AT - timedelta(hours=8 * index)) for index in range(100)]
    forward = evaluate_bybit(funding_rows=funding_rows)
    reverse = evaluate_bybit(funding_rows=list(reversed(funding_rows)))
    for result in (forward, reverse):
        payload = result.to_dict()
        assert result.classification == "eligible_no_conflict_detected"
        assert any(item["role"] == "instrument" for item in payload["lineage"])
        assert ("selected_current_oi", "110") in roles(result)
        assert ("selected_previous_oi", "100") in roles(result)
        assert any(item["role"] == "selected_funding" and item["metric_time"] == FUNDING_AT.isoformat() for item in payload["lineage"])
        assert result.lineage_omitted_count == 71
        assert len(result.lineage) == 32
        oldest = (FUNDING_AT - timedelta(hours=8 * 99)).isoformat()
        assert oldest not in {item["metric_time"] for item in payload["lineage"]}
        restored = DerivativesSourceTruthResult.from_dict(payload)
        assert restored.lineage_omitted_count == 71
        assert restored.to_dict()["lineage"] == payload["lineage"]
        assert_cannot_confirm(result)

    series = [(CUTOFF - timedelta(minutes=5 * (39 - index)), str(100 + index)) for index in range(40)]
    change, change_reasons = native_position_change_pct(Decimal("139"), Decimal("138"))
    assert change_reasons == ()
    for rows in (series, list(reversed(series))):
        wide = evaluate_bybit(oi_rows=rows)
        assert wide.oi_change_pct == format(change, "f")
        assert ("selected_current_oi", "139") in roles(wide)
        assert ("selected_previous_oi", "138") in roles(wide)
        assert "100" not in {item["raw_value"] for item in wide.to_dict()["lineage"] if item["role"] == "selected_current_oi"}
        assert wide.lineage_omitted_count > 0


def test_predecessor_conflict_lineage_names_the_rejected_rows() -> None:
    history = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=oi_history_payload([(PREVIOUS, "100", None), (PREVIOUS, "50", None), (CUTOFF, "110", None)]),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    current = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": "110", "time": millis(CUTOFF)},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=current,
        open_interest_history=history,
        funding_history=binance_funding(funding_payload("0.0001", FUNDING_AT)),
    )
    payload = result.to_dict()
    assert result.oi_change_pct == NA
    assert "duplicate_conflict" in result.reason_codes
    values = {item["raw_value"] for item in payload["lineage"]}
    assert {"110", "100", "50"} <= values
    assert "selected_current_oi" not in {item["role"] for item in payload["lineage"]}
    assert any(item["response_time"] is None and item["raw_value"] == "110" for item in payload["lineage"])
    assert "1970-01-01" not in json.dumps(payload)


def test_lineage_round_trip_and_dev_artifact_keep_bounds(tmp_path) -> None:
    result = evaluate_bybit(ratio_rows=[ratio_row()])
    payload = result.to_dict()
    assert "lineage_omitted_count" in payload
    restored = DerivativesSourceTruthResult.from_dict(payload)
    assert restored.lineage_omitted_count == result.lineage_omitted_count
    assert restored.mode == "shadow"
    assert restored.enforcement == "not_active"
    assert restored.validated_confirmation is False
    target = tmp_path / "c1-c3-shadow.json"
    write_dev_shadow_artifact(target, result)
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["lineage"] == payload["lineage"]
    assert artifact["lineage_omitted_count"] == payload["lineage_omitted_count"]
    assert artifact["mode"] == "shadow"


def test_public_serialization_persistence_and_identity_exclude_lineage(tmp_path) -> None:
    assert SCHEMA_VERSION == 27
    assert ScannerRunConfig.model_fields["derivatives_source_truth_shadow"].default is False
    assert ScannerSymbolResult.model_fields["derivatives_source_truth_shadow"].exclude is True
    connection = connect_database(tmp_path / "c1-c3-schema.sqlite")
    try:
        initialize_database(connection)
        assert identify_schema_version(connection) == 27
        script = "\n".join(row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        assert "derivatives_source_truth" not in script
        assert "lineage_omitted_count" not in script
    finally:
        connection.close()


def test_evaluation_error_still_keeps_input_lineage() -> None:
    class Boom(dict):
        def __getattr__(self, name: str):
            raise RuntimeError(name)

    failed = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        open_interest_history=bybit_open_interest(),
        instrument=Boom(),  # type: ignore[arg-type]
    )
    assert failed.classification == "evaluation_error"
    assert failed.validated_confirmation is False
    assert any(item["endpoint"] == "/v5/market/open-interest" for item in failed.to_dict()["lineage"])


def test_synthetic_share_uncertainty_crossing_zero_is_unverified() -> None:
    """Not an exchange payload: stated precision is coarser than the numeric share."""

    long_part = parse_quantity(
        "0.99",
        raw_field="longAccount",
        unit=UNIT_ACCOUNT_RATIO,
        counting_convention=COUNT_NOT_APPLICABLE,
    )
    short_part = replace(
        parse_quantity(
            "0.01",
            raw_field="shortAccount",
            unit=UNIT_ACCOUNT_RATIO,
            counting_convention=COUNT_NOT_APPLICABLE,
        ),
        raw_value="0.1",
    )
    fact, reasons = _account_ratio_quantity(
        "99",
        ratio_field="longShortRatio",
        long_part=long_part,
        short_part=short_part,
    )
    assert "account_share_precision_unjustified" in reasons
    assert fact.numeric_value is None
    assert fact.quality == QUALITY_UNVERIFIED
    assert "ratio_component_conflict" not in reasons


@pytest.mark.parametrize(
    ("venue", "extra_fields", "expected_reasons", "expected_components"),
    [
        (
            "bybit",
            {"rateType": "Regular"},
            ("unexpected_funding_subtype",),
            [{"raw_field": "rateType", "raw_value": "Regular"}],
        ),
        (
            "bybit",
            {"fundingRateType": "Regular"},
            ("unexpected_funding_subtype",),
            [{"raw_field": "fundingRateType", "raw_value": "Regular"}],
        ),
        (
            "binance",
            {"fundingRateType": "regular"},
            ("unexpected_funding_subtype",),
            [{"raw_field": "fundingRateType", "raw_value": "regular"}],
        ),
        (
            "binance",
            {"rateType": "Regular", "fundingRateType": "predicted"},
            ("unexpected_funding_subtype", "contradictory_funding_evidence"),
            [
                {"raw_field": "rateType", "raw_value": "Regular"},
                {"raw_field": "fundingRateType", "raw_value": "predicted"},
            ],
        ),
        (
            "binance",
            {"rateType": "Regular", "fundingRateType": "Special"},
            ("unsupported_funding_subtype", "unexpected_funding_subtype", "contradictory_funding_evidence"),
            [
                {"raw_field": "rateType", "raw_value": "Regular"},
                {"raw_field": "fundingRateType", "raw_value": "Special"},
            ],
        ),
        (
            "binance",
            {"rateType": "Regular", "fundingRateType": "unexpected"},
            ("unexpected_funding_subtype", "contradictory_funding_evidence"),
            [
                {"raw_field": "rateType", "raw_value": "Regular"},
                {"raw_field": "fundingRateType", "raw_value": "unexpected"},
            ],
        ),
        (
            "binance",
            {"rateType": "Regular", "fundingRateType": "Regular"},
            ("unexpected_funding_subtype",),
            [
                {"raw_field": "rateType", "raw_value": "Regular"},
                {"raw_field": "fundingRateType", "raw_value": "Regular"},
            ],
        ),
        (
            "binance",
            {"rateType": ""},
            ("unexpected_funding_subtype",),
            [{"raw_field": "rateType", "raw_value": NA}],
        ),
        (
            "binance",
            {"rateType": None},
            ("unexpected_funding_subtype",),
            [{"raw_field": "rateType", "raw_value": NA}],
        ),
        (
            "binance",
            {"rateType": 123},
            ("unexpected_funding_subtype",),
            [{"raw_field": "rateType", "raw_value": "123"}],
        ),
        (
            "binance",
            {"rateType": "settled_funding"},
            ("unexpected_funding_subtype",),
            [{"raw_field": "rateType", "raw_value": "settled_funding"}],
        ),
    ],
)
def test_d1_funding_subtype_scope_and_sibling_declarations(
    tmp_path,
    venue: str,
    extra_fields: dict,
    expected_reasons: tuple[str, ...],
    expected_components: list[dict[str, str]],
) -> None:
    if venue == "bybit":
        row = fund_row(**extra_fields)
        batch = bybit_funding([row])
        result = evaluate_bybit(funding_rows=[row])
    else:
        rows = funding_payload("0.0001", FUNDING_AT)
        rows[0].update(extra_fields)
        batch = binance_funding(rows)
        result = evaluate_binance_funding(rows)

    obs = batch.observations[0]
    assert obs.semantic_quality == QUALITY_UNVERIFIED
    assert obs.rate_kind != RATE_SETTLED_FUNDING
    for code in expected_reasons:
        assert code in obs.reason_codes
        assert code in result.reason_codes
    assert result.classification == "unverified"
    assert result.funding_label != RATE_SETTLED_FUNDING
    assert result.funding_rate == NA
    assert result.supports_long == "unverified"
    assert result.supports_short == "unverified"
    assert_cannot_confirm(result)

    payload = result.to_dict()
    excluded = [item for item in payload["lineage"] if item["role"] == "excluded_funding"]
    assert len(excluded) == 1
    assert excluded[0]["components"] == expected_components
    for code in expected_reasons:
        assert code in excluded[0]["reason_codes"]

    restored = DerivativesSourceTruthResult.from_dict(payload)
    assert restored.to_dict() == payload
    artifact_path = tmp_path / "d1-subtype-artifact.json"
    write_dev_shadow_artifact(artifact_path, result)
    artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact_payload == payload


def test_d1_field_order_and_duplicate_row_order_invariance() -> None:
    base = funding_payload("0.0001", FUNDING_AT)[0]
    row_forward = {**base, "rateType": "Regular", "fundingRateType": "predicted"}
    row_reverse = {"fundingRateType": "predicted", "rateType": "Regular", **base}
    res_forward = evaluate_binance_funding([row_forward])
    res_reverse = evaluate_binance_funding([row_reverse])
    assert res_forward.to_dict() == res_reverse.to_dict()

    bybit_forward = evaluate_bybit(funding_rows=[fund_row(rateType="Regular", fundingRateType="Special")])
    bybit_reverse = evaluate_bybit(funding_rows=[fund_row(fundingRateType="Special", rateType="Regular")])
    assert bybit_forward.to_dict() == bybit_reverse.to_dict()

    valid_row = {**base, "rateType": "Regular"}
    invalid_bybit_style = {**base, "fundingRateType": "regular"}
    special_row = {**base, "rateType": "Special"}
    for left, right in (
        (valid_row, invalid_bybit_style),
        (special_row, invalid_bybit_style),
        (valid_row, row_forward),
    ):
        first = evaluate_binance_funding([left, right])
        second = evaluate_binance_funding([right, left])
        assert outcome(first) == outcome(second)
        assert first.to_dict() == second.to_dict()
        assert first.classification == "unverified"
        assert_cannot_confirm(first)

