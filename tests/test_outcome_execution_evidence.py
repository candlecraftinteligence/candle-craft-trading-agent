"""Supplied candle-integrity uncertainty stays visible beside plan attribution."""

from __future__ import annotations

import copy
import json

import pytest

from app.analytics.outcome_ownership import project_outcome_ownership
from tests.test_outcome_ownership_contract import _decision, _latched, _progress_row, _provenance


def _terminal_row(record, **updates):
    return _progress_row(
        record, plan_version_id=record.plan_version_id, tracking_start_at=_decision(0),
        entry_at=_decision(1), tp1_at=_decision(2), tp3_at=_decision(4),
        terminal_outcome="TP_HIT", **updates,
    )


def test_unverified_terminal_is_stratified_separately_from_verified_identity():
    record = _latched()
    row = _terminal_row(record, integrity_status="Unverified", diagnostic="irrecoverable_cursor_interval:fixture")
    before = copy.deepcopy(row)
    report = project_outcome_ownership(lifecycle_records=[record], progress_rows=[row], provenance=_provenance())
    evaluation = report["evaluations"][0]
    assert evaluation["integrity_status"] == "verified_attribution"
    assert evaluation["execution_evidence"]["stratum"] == "reported_unverified"
    interpretation = report["plan_interpretations"][0]
    assert interpretation["economic_status"] == "tp3_terminal"
    assert interpretation["execution_evidence"]["stratum"] == "reported_unverified"
    assert report["counts"]["interpretable_plan_outcomes"]["execution_evidence_strata"] == {"reported_unverified": 1}
    assert report["unavailable_metrics"]["expectancy"]["value"] is None
    assert report["execution_evidence_policy"]["historical_completeness"] == "Unverified"
    assert row == before


@pytest.mark.parametrize(("status", "stratum"), [
    ("Verified", "reported_verified"), ("Unverified", "reported_unverified"),
    ("Failed", "reported_failed"), (None, "unavailable"),
    ("N/A", "unavailable"), ("Definitely Verified", "unrecognized"),
    (False, "unrecognized"), (0, "unrecognized"), ("   ", "unavailable"),
])
def test_source_integrity_is_a_claim_not_causal_completeness(status, stratum):
    record = _latched()
    report = project_outcome_ownership(
        lifecycle_records=[record], progress_rows=[_terminal_row(record, integrity_status=status)],
        provenance=_provenance(coverage_complete=True),
    )
    evidence = report["evaluations"][0]["execution_evidence"]
    assert evidence["stratum"] == stratum
    assert evidence["historical_completeness"] == "Unverified"
    expected = "N/A" if status is None or isinstance(status, str) and not status.strip() else status
    assert evidence["supplied_rows"][0]["integrity_status"] == expected


def test_conflicting_supplied_statuses_and_timestamps_are_retained_without_repair():
    record = _latched()
    first = _terminal_row(
        record, integrity_status="Unverified", diagnostic="missing_entry_tracking_confirmation_boundary",
        evaluation_cursor_close_at="2026-01-01T00:05:00+00:00",
        metadata_json=json.dumps({"source": "historical_exchange_reconstruction"}),
    )
    second = {**first, "integrity_status": "Verified", "diagnostic": "N/A"}
    original = copy.deepcopy([first, second])
    report = project_outcome_ownership(lifecycle_records=[record], progress_rows=[first, second], provenance=_provenance())
    evidence = report["evaluations"][0]["execution_evidence"]
    assert evidence["stratum"] == "mixed"
    assert {row["integrity_status"] for row in evidence["supplied_rows"]} == {"Verified", "Unverified"}
    assert {row["evaluation_cursor_close_at"] for row in evidence["supplied_rows"]} == {first["evaluation_cursor_close_at"]}
    assert [first, second] == original
    reversed_report = project_outcome_ownership(lifecycle_records=[record], progress_rows=[second, first], provenance=_provenance())
    assert reversed_report == report


def test_verified_status_with_a_gap_diagnostic_is_a_distinct_stratum():
    record = _latched()
    report = project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[_terminal_row(record, integrity_status="Verified", diagnostic="missing_entry_tracking_confirmation_boundary")],
        provenance=_provenance(),
    )
    assert report["evaluations"][0]["execution_evidence"]["stratum"] == "reported_verified_with_diagnostic"


def test_lineage_timestamps_and_namespaces_are_not_relabelled():
    from datetime import datetime, timezone
    from decimal import Decimal
    from app.analytics.outcome_ownership import dumps_outcome_ownership_report

    record = _latched()
    metadata = {"source": "historical_exchange_reconstruction", "received_at": "2026-10-23T10:00:00+02:00",
                "source_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "fixture_amount": Decimal("1.25")}
    reconstructed = _terminal_row(record, integrity_status="Unverified", source_namespace="reconstructed",
                                   metadata=metadata, evaluation_cursor_close_at="2026-01-01T01:05:00+01:00")
    live = {**reconstructed, "source_namespace": "prospective", "integrity_status": "Verified", "metadata": {"source": "prospective"}}
    inputs = copy.deepcopy([reconstructed, live])
    report = project_outcome_ownership(
        lifecycle_records=[{**record.model_dump(mode="json"), "source_namespace": ns} for ns in ("reconstructed", "prospective")],
        progress_rows=[reconstructed, live], provenance=_provenance(),
    )
    strata = {row["source_namespace"]: row["execution_evidence"]["stratum"] for row in report["plan_interpretations"]}
    assert strata == {"reconstructed": "reported_unverified", "prospective": "reported_verified"}
    retained = next(row for row in report["evaluations"] if row["source_namespace"] == "reconstructed")["execution_evidence"]["supplied_rows"][0]
    assert retained["evaluation_cursor_close_at"] == reconstructed["evaluation_cursor_close_at"]
    assert retained["metadata"]["source"] == "historical_exchange_reconstruction"
    assert retained["metadata"]["received_at"] == metadata["received_at"]
    assert [reconstructed, live] == inputs
    assert json.loads(dumps_outcome_ownership_report(report))["execution_evidence_policy"]["historical_completeness"] == "Unverified"


def test_absent_progress_is_not_a_zero_integrity_observation():
    report = project_outcome_ownership(lifecycle_records=[_latched()], provenance=_provenance())
    assert report["evaluations"][0]["execution_evidence"]["stratum"] == "no_progress_supplied"
    assert report["counts"]["execution_evidence_strata"]["evaluation_groups"] == {"no_progress_supplied": 1}
