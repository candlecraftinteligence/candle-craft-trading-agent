from __future__ import annotations

import json

from app.analytics.evidence_contract import (
    CONTRACT_VERSION,
    UNAVAILABLE,
    UNSAFE,
    dumps_evidence_contract,
    evidence_contract_payload,
    required_entity_names,
    required_metric_names,
    required_timestamp_names,
)


def test_evidence_contract_is_stable_and_complete() -> None:
    payload = evidence_contract_payload()
    encoded = dumps_evidence_contract()
    assert encoded == dumps_evidence_contract()
    loaded = json.loads(encoded)
    assert loaded["contract_version"] == CONTRACT_VERSION
    assert loaded == json.loads(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    for name in required_entity_names():
        assert name in loaded["entities"]
        entity = loaded["entities"][name]
        assert "current_authoritative_id" in entity
        assert "proposed_future_id" in entity
        assert entity["current_authoritative_id"] != entity.get("proposed_future_id") or name in {
            "READINESS",
            "QUALITY",
        }
    for name in required_metric_names():
        metric = loaded["metrics"][name]
        assert metric["unique_economic_plans_counted"] == "no"
        assert "counting_unit" in metric
        assert "trust_status" in metric
    for name in required_timestamp_names():
        assert name in loaded["timestamps"]
        assert "as_of_suitable" in loaded["timestamps"][name]


def test_unique_trade_count_is_unavailable_not_distinct() -> None:
    payload = evidence_contract_payload()
    assert payload["unique_trade_count"]["status"] == UNAVAILABLE
    assert "DISTINCT" in payload["unique_trade_count"]["reason"]


def test_fixture_examples_keep_counting_units_separate() -> None:
    examples = evidence_contract_payload()["counting_unit_examples"]
    assert examples["repeated_observations"]["unit"] == "observation"
    assert "not two trades" in examples["repeated_observations"]["example"]
    assert "not three completed trades" in examples["outcome_fanout"]["example"]
    assert examples["boundary_window"]["unit"] == "half-open [start, cutoff)"
    assert evidence_contract_payload()["entities"]["ACTIVATION"]["research_statistics_safe"] == UNSAFE
    assert evidence_contract_payload()["entities"]["MANUAL FILL"]["research_statistics_safe"] == UNAVAILABLE
    assert evidence_contract_payload()["metrics"]["actionable_setups"]["trust_status"] == UNSAFE
    assert evidence_contract_payload()["metrics"]["valid_activations"]["trust_status"] == UNSAFE
    assert "deprecated as an economic research metric" in evidence_contract_payload()["metrics"]["valid_activations"]["limitations"].lower()
    accounting = evidence_contract_payload()["activation_accounting"]
    assert accounting["feeds_operational_decisions"] is False
    assert accounting["schema_version_unchanged"] is True
    assert accounting["unsupported_occurrence_metrics"]["fill_occurrence_count"] == UNAVAILABLE
    assert accounting["unsupported_occurrence_metrics"]["manual_fill_count"] == UNAVAILABLE
    assert "ENTRY_ACTIVATED" in examples["trigger_touch_activation_fill"]["example"]
    assert "identical to actionable_a_grade_setups" in evidence_contract_payload()["metrics"]["actionable_setups"]["inclusion_rules"]
